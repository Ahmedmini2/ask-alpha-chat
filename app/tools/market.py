import json
from typing import Any, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.tools.registry import Tool, registry

# Market medians are AED per square METRE; our project_units prices are AED per
# square FOOT. 1 m² = 10.7639 ft², so AED/sqft = AED/sqm / 10.7639.
SQM_PER_SQFT = 10.7639


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
    query = (args.get("query") or "").strip()
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
