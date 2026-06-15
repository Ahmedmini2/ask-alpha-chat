import json
import re
from typing import Any, Optional
from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project, ProjectAlphaVerdict
from app.tools.registry import Tool, registry

# Market medians are AED per square METRE; our project_units prices are AED per
# square FOOT. 1 m² = 10.7639 ft², so AED/sqft = AED/sqm / 10.7639.
SQM_PER_SQFT = 10.7639


def _clean_area(area: str) -> str:
    """Collapse whitespace (incl. non-breaking spaces) so area names match. Some
    district strings store a U+00A0 between words (e.g. 'Downtown\\xa0Dubai') which
    defeats get_market_sentiment's name resolution."""
    return re.sub(r"\s+", " ", (area or "").replace(" ", " ")).strip()


def _aed_sqft(rate_sqm: Any) -> Optional[float]:
    if rate_sqm is None:
        return None
    try:
        return round(float(rate_sqm) / SQM_PER_SQFT, 2)
    except (TypeError, ValueError):
        return None


def _momentum_phrase(s: dict) -> str:
    """Plain-language read of the 90d-vs-prev-90d rate momentum + activity."""
    mom = s.get("rate_momentum_pct")
    label = s.get("activity_label") or "unknown"
    if mom is None:
        return f"Activity is {label}; momentum data unavailable."
    direction = "up" if mom > 0 else "down" if mom < 0 else "flat"
    return (
        f"Median rate/sqm is {direction} {abs(mom):.1f}% over the last 90 days vs the "
        f"prior 90 days; market activity is '{label}'."
    )


async def _recent_comparables(db: AsyncSession, s: dict, limit: int) -> list[dict]:
    """Most recent transactions for the matched area, for grounding the numbers."""
    level = s.get("match_level")
    name = s.get("matched_name")
    if not name:
        return []
    col = {"community": "community", "district": "district",
           "building": "building_name", "project": "project"}.get(level, "district")
    rows = (await db.execute(
        text(f"""
            SELECT txn_date, property_type, layout, size_sqm, price_aed,
                   rate_aed_sqm, sale_type, building_name, project
            FROM market_transactions
            WHERE {col} ILIKE :name AND price_aed > 0
            ORDER BY txn_date DESC
            LIMIT :lim
        """),
        {"name": name, "lim": limit},
    )).mappings().all()
    out = []
    for r in rows:
        out.append({
            "txn_date": r["txn_date"].isoformat() if r["txn_date"] else None,
            "property_type": r["property_type"],
            "layout": r["layout"],
            "size_sqft": round(float(r["size_sqm"]) * SQM_PER_SQFT, 0) if r["size_sqm"] else None,
            "price_aed": float(r["price_aed"]) if r["price_aed"] is not None else None,
            "rate_aed_sqft": _aed_sqft(r["rate_aed_sqm"]),
            "sale_type": r["sale_type"],
            "building": r["building_name"],
            "project": r["project"],
        })
    return out


async def get_market_intelligence_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = _clean_area(args.get("query") or "")
    if not query:
        return {"error": "query is required (an area, community, district, or project name)"}
    include_comparables = bool(args.get("include_comparables", False))

    row = (await db.execute(
        text("SELECT get_market_sentiment(:q) AS s"), {"q": query}
    )).mappings().first()
    s = row["s"] if row else None
    # asyncpg may hand back jsonb as a string depending on codec; normalize.
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            s = None

    # A miss is {"match": null, ...}; a hit has matched_name/match_level and no
    # "match" key — so detect on matched_name, not on a "match" key.
    if not isinstance(s, dict) or s.get("matched_name") is None:
        return {
            "found": False,
            "query": query,
            "message": "We don't have transaction data for that area in our system yet.",
        }

    result = {
        "found": True,
        "query": query,
        "matched_name": s.get("matched_name"),
        "match_level": s.get("match_level"),
        "city": s.get("city"),
        "district": s.get("district"),
        "community": s.get("community"),
        "median_price_aed_12m": float(s["median_price_aed_12m"]) if s.get("median_price_aed_12m") is not None else None,
        "median_rate_aed_sqm_12m": float(s["median_rate_sqm_12m"]) if s.get("median_rate_sqm_12m") is not None else None,
        "median_rate_aed_sqft_12m": _aed_sqft(s.get("median_rate_sqm_12m")),
        "median_rate_aed_sqft_90d": _aed_sqft(s.get("median_rate_sqm_90d")),
        "rate_momentum_pct": s.get("rate_momentum_pct"),
        "activity_label": s.get("activity_label"),
        "pct_offplan_12m": s.get("pct_offplan_12m"),
        "txn_12m": s.get("txn_12m"),
        "txn_90d": s.get("txn_90d"),
        "txn_prev_90d": s.get("txn_prev_90d"),
        "last_txn_date": s.get("last_txn_date"),
        "summary": _momentum_phrase(s),
    }
    if include_comparables:
        result["recent_comparables"] = await _recent_comparables(db, s, limit=5)
    return result


async def _resolve_project(db: AsyncSession, args: dict):
    """Resolve a project from project_id or a name (exact ILIKE, then trigram)."""
    pid = args.get("project_id")
    if pid is not None:
        return (await db.execute(select(Project).where(Project.id == int(pid)))).scalar_one_or_none()
    name = (args.get("project_name") or "").strip()
    if not name:
        return None
    p = (await db.execute(
        select(Project).where(Project.name.ilike(f"%{name}%")).limit(1)
    )).scalar_one_or_none()
    if p:
        return p
    # Trigram fallback, but conservative — a low threshold matches on shared words
    # like "Tower" and returns an unrelated project.
    sim = func.similarity(Project.name, name)
    return (await db.execute(
        select(Project).where(sim > 0.45).order_by(sim.desc()).limit(1)
    )).scalar_one_or_none()


async def _sentiment(db: AsyncSession, area: str) -> Optional[dict]:
    area = _clean_area(area)
    if not area:
        return None
    row = (await db.execute(text("SELECT get_market_sentiment(:q) AS s"), {"q": area})).mappings().first()
    s = row["s"] if row else None
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            s = None
    if isinstance(s, dict) and s.get("matched_name") is not None:
        return s
    return None


async def _analyze_one(db: AsyncSession, p: Project) -> dict:
    """Core investment primitive for one project: asking rate vs area market,
    momentum, supply, payment plan, and a clearly-labeled rental-yield estimate."""
    agg = (await db.execute(
        text("""
            SELECT
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_area)
                FILTER (WHERE price_per_area > 0)            AS median_sqft,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                FILTER (WHERE price > 0)                      AS median_price,
              mode() WITHIN GROUP (ORDER BY lower(unit_type)) AS dominant_type,
              count(*) FILTER (WHERE price > 0)               AS priced_units
            FROM project_units WHERE project_id = :id
        """),
        {"id": p.id},
    )).mappings().first()

    asking_sqft = float(agg["median_sqft"]) if agg and agg["median_sqft"] is not None else None
    median_price = float(agg["median_price"]) if agg and agg["median_price"] is not None else None
    dominant_type = (agg["dominant_type"] if agg else None) or "default"

    # Market context for the project's district.
    s = await _sentiment(db, p.district or "")
    market_sqft = _aed_sqft(s.get("median_rate_sqm_12m")) if s else None

    premium_pct = None
    valuation = "unknown"
    if asking_sqft and market_sqft:
        premium_pct = round((asking_sqft - market_sqft) / market_sqft * 100, 1)
        if premium_pct >= 15:
            valuation = "above area median"
        elif premium_pct <= -15:
            valuation = "below area median"
        else:
            valuation = "in line with area median"

    # Rental yield ESTIMATE (no observed rental data — labeled as estimate).
    yr = (await db.execute(
        text("""SELECT gross_yield_low, gross_yield_high, note
                FROM investment_yield_assumptions WHERE property_type = :t"""),
        {"t": dominant_type},
    )).mappings().first()
    if not yr:
        yr = (await db.execute(
            text("""SELECT gross_yield_low, gross_yield_high, note
                    FROM investment_yield_assumptions WHERE property_type = 'default'"""),
        )).mappings().first()
    yield_low = float(yr["gross_yield_low"]) if yr else None
    yield_high = float(yr["gross_yield_high"]) if yr else None
    est_rent = None
    if median_price and yield_low and yield_high:
        est_rent = {
            "annual_low": round(median_price * yield_low / 100, 0),
            "annual_high": round(median_price * yield_high / 100, 0),
        }

    return {
        "project_id": p.id,
        "name": p.name,
        "developer": p.developer.name if p.developer else None,
        "district": p.district,
        "sale_status": p.sale_status,
        "completion_quarter": p.completion_quarter,
        "post_handover_plan": bool(p.post_handover) if p.post_handover is not None else None,
        "units_count": p.units_count,
        "asking_rate_aed_sqft": round(asking_sqft, 0) if asking_sqft else None,
        "median_unit_price_aed": round(median_price, 0) if median_price else None,
        "dominant_unit_type": dominant_type,
        "market": ({
            "matched_name": s.get("matched_name"),
            "median_rate_aed_sqft_12m": market_sqft,
            "rate_momentum_pct": s.get("rate_momentum_pct"),
            "activity_label": s.get("activity_label"),
            "pct_offplan_12m": s.get("pct_offplan_12m"),
            "txn_12m": s.get("txn_12m"),
        } if s else None),
        "valuation_vs_market": valuation,
        "premium_to_market_pct": premium_pct,
        "rental_yield_estimate": ({
            "gross_yield_low_pct": yield_low,
            "gross_yield_high_pct": yield_high,
            "estimated_annual_rent_aed": est_rent,
            "basis": "ESTIMATE — market-typical band by unit type; no observed rental data yet",
        } if yield_low else None),
        "data_gaps": [g for g in [
            None if asking_sqft else "no priced units to derive an asking rate",
            None if s else "no market transaction data for this district",
        ] if g],
    }


async def analyze_investment_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    p = await _resolve_project(db, args)
    if p is None:
        return {"found": False, "message": "We don't have that project in our system yet."}
    analysis = await _analyze_one(db, p)
    return {"found": True, **analysis}


async def compare_projects_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    ids = args.get("project_ids") or []
    if not isinstance(ids, list) or not ids:
        return {"error": "project_ids (a list of 2-3 numeric project IDs) is required"}
    ids = ids[:3]
    analyses = []
    for pid in ids:
        p = (await db.execute(select(Project).where(Project.id == int(pid)))).scalar_one_or_none()
        if p is not None:
            analyses.append(await _analyze_one(db, p))
    if not analyses:
        return {"found": False, "message": "None of those project IDs were found."}
    # Attach the Alpha Verdict to each compared project so the conviction score is visible on every
    # card. A user-picked head-to-head keeps the user's chosen order — we don't re-rank it (the
    # conviction-first rule governs discovery lists, not a deliberately-ordered comparison).
    aids = [a["project_id"] for a in analyses if a.get("project_id") is not None]
    vmap: dict = {}
    if aids:
        vrows = (await db.execute(
            select(ProjectAlphaVerdict.project_id, ProjectAlphaVerdict.verdict,
                   ProjectAlphaVerdict.conviction)
            .where(ProjectAlphaVerdict.project_id.in_(aids))
        )).all()
        vmap = {pid: (verd, float(conv)) for pid, verd, conv in vrows}
    for a in analyses:
        _vc = vmap.get(a.get("project_id"))
        a["verdict"] = _vc[0] if _vc else None
        a["conviction"] = round(_vc[1]) if _vc else None
    return {"found": True, "count": len(analyses), "projects": analyses}


registry.register(Tool(
    name="analyze_investment",
    description=(
        "Produce an investment analysis for ONE project: its median asking rate (AED/sqft) vs the "
        "area's market median, the premium/discount to market, 90-day price momentum and activity "
        "(hot/cooling), off-plan share, supply (unit count), whether it has a post-handover payment "
        "plan, and a clearly-labeled rental-yield ESTIMATE. Use this when the user asks whether a "
        "project is a good investment, worth buying, good value, or good ROI. Identify the project by "
        "project_id (preferred) or project_name. Present the numbers and reasoning; never invent data — "
        "report any data_gaps the tool returns."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Numeric project ID (preferred). Get it from search_projects/search_units."},
            "project_name": {"type": "string", "description": "Project name, if the ID isn't known. Resolved by fuzzy match."},
        },
        "required": [],
    },
    handler=analyze_investment_handler,
))

registry.register(Tool(
    name="compare_projects",
    description=(
        "Compare 2-3 projects head-to-head on the same investment metrics as analyze_investment "
        "(asking rate vs market, premium/discount, momentum, activity, supply, payment plan, yield "
        "estimate). Use when the user asks to compare projects or pick between them. Pass project_ids."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "2-3 numeric project IDs to compare.",
            },
        },
        "required": ["project_ids"],
    },
    handler=compare_projects_handler,
))


registry.register(Tool(
    name="get_market_intelligence",
    description=(
        "Get real transaction-based market data for a UAE area, community, district, or project, "
        "from 680k+ recorded deals. Returns median price and median rate (AED per sqm and per sqft) "
        "over the last 12 months, 90-day price momentum, transaction counts (12m / 90d / prior 90d), "
        "off-plan share, and an activity label (hot / healthy / cooling / quiet). Set include_comparables=true "
        "to also get the most recent individual sales. Use this whenever the user asks about market "
        "trends, prices in an area, how a location is performing, or whether a market is rising or cooling. "
        "If found=false, we have no data for that area — say so, do not guess."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Area / community / district / project name, e.g. 'Palm Jumeirah', 'Dubai Marina', 'Business Bay'.",
            },
            "include_comparables": {
                "type": "boolean",
                "description": "If true, also return the 5 most recent individual transactions in that area.",
                "default": False,
            },
        },
        "required": ["query"],
    },
    handler=get_market_intelligence_handler,
))
