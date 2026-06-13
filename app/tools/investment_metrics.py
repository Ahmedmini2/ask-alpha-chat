"""get_investment_metrics — the website's investment summary numbers on demand.

Returns Net Yield, Capital (annual) Appreciation, 5-year projected value & gain,
Area Average Rent Return and Time-to-Sell in Area for a project (or a bare
price + community), computed with the same area model the public website uses.
These are area-MODEL ESTIMATES — the handler returns a `basis` string the
assistant must surface so they're never presented as live per-property data.
"""
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import property_metrics as metrics
from app.db.models import Project
from app.tools.registry import Tool, registry

SQM_PER_SQFT = 10.7639


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _resolve_project(db: AsyncSession, args: dict) -> Optional[Project]:
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
    sim = func.similarity(Project.name, name)
    return (await db.execute(
        select(Project).where(sim > 0.45).order_by(sim.desc()).limit(1)
    )).scalar_one_or_none()


async def _entry_unit(db: AsyncSession, project: Project) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(price, beds, size_sqft) for the cheapest priced unit, falling back to the
    project's own min_price / min_size."""
    row = (await db.execute(text("""
        SELECT bedrooms,
               COALESCE(price_from, price)             AS price,
               COALESCE(size_from, size)               AS size,
               COALESCE(NULLIF(lower(area_unit),'none'), :pu) AS area_unit
        FROM project_units
        WHERE project_id = :id AND COALESCE(price_from, price) > 0
        ORDER BY COALESCE(price_from, price) ASC
        LIMIT 1
    """), {"id": project.id, "pu": (project.area_unit or "sqft")})).mappings().first()
    if row and _f(row["price"]):
        size = _f(row["size"])
        if size and str(row["area_unit"] or "").startswith("sqm"):
            size *= SQM_PER_SQFT
        return _f(row["price"]), _f(row["bedrooms"]), (size if (size and size > 0) else None)
    # project-level fallback
    price = _f(project.min_price)
    size = _f(project.min_size)
    if size and (project.area_unit or "").lower().startswith("sqm"):
        size *= SQM_PER_SQFT
    return price, None, (size if (size and size > 0) else None)


async def get_investment_metrics_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    project = None
    if args.get("project_id") is not None or args.get("project_name"):
        project = await _resolve_project(db, args)
        if project is None:
            return {"found": False, "message": "We don't have that project in our system yet."}

    # Resolve the inputs: explicit args win, then the project's entry unit.
    price = _f(args.get("price"))
    beds = _f(args.get("beds"))
    sqft = _f(args.get("sqft"))
    is_rent = bool(args.get("is_rent", False))

    if project is not None:
        ep, eb, es = await _entry_unit(db, project)
        price = price if price is not None else ep
        beds = beds if beds is not None else eb
        sqft = sqft if sqft is not None else es
        community = args.get("community") or project.district or project.city
        area_inputs = await metrics.gather_area_inputs(db, project)
    else:
        community = args.get("community")
        area_inputs = await metrics.gather_area_inputs_by_area(db, community or "")

    if not price or price <= 0:
        return {"found": False, "message": (
            "I need a price to estimate these metrics — that project has no priced "
            "units in our system. Tell me a price (and ideally beds + size) and I'll compute them."
            if project is not None else
            "A price is required (in AED). Optionally pass beds, sqft and community."
        )}

    m = metrics.compute_metrics(
        price,
        beds=beds, sqft=sqft, community=community,
        area_yield=area_inputs["area_yield"],
        area_appreciation=area_inputs["area_appreciation"],
        area_ppsf=area_inputs["area_ppsf"],
        activity_label=area_inputs["activity_label"],
        is_rent=is_rent,
    )
    if m is None:
        return {"found": False, "message": "Couldn't compute metrics for those inputs."}

    return {
        "found": True,
        "project_id": project.id if project else None,
        "project_name": project.name if project else None,
        "community": community,
        "community_modeled_as": m["community_matched"],
        "used_area_fallback": m["used_fallback"],
        "inputs": {"price_aed": round(price, 0), "beds": beds, "size_sqft": round(sqft, 0) if sqft else None},
        "metrics": {
            "net_yield_pct": m["net_yield_pct"],
            "area_avg_rent_return_pct": m["area_avg_rent_return_pct"],
            "annual_appreciation_pct": m["annual_appreciation_pct"],
            "y5_projected_value_aed": m["y5_projected_value_aed"],
            "five_year_gain_pct": m["five_year_gain_pct"],
            "time_to_sell_days": m["time_to_sell_days"],
            "price_per_sqft_aed": m["price_per_sqft_aed"],
            "vs_area_price_pct": m["vs_area_price_pct"],
        },
        "area_momentum_pct": area_inputs["momentum_pct"],
        "sources": m["sources"],
        "basis": metrics.BASIS,
    }


registry.register(Tool(
    name="get_investment_metrics",
    description=(
        "Compute the website's investment summary metrics for a project: Net Yield, "
        "Capital/Annual Appreciation, 5-Year projected value & gain, Area Average Rent "
        "Return, and Time-to-Sell in Area (plus price/sqft and vs-area-price). Use this "
        "whenever the user asks for any of those figures — 'what's the net yield', "
        "'how much will it appreciate', 'what's it worth in 5 years', 'how long to sell in "
        "that area', 'area rental return'. Identify the project by project_id (preferred) or "
        "project_name; or pass a raw price (+ beds, sqft, community) for a hypothetical. These "
        "are AREA-MODEL ESTIMATES — present them as estimates and surface the returned 'basis'. "
        "Real area data (rental-yield band, district price/sqft, activity) is used where we have "
        "it; otherwise an area model fills in, with a Dubai baseline for unmodeled communities."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Numeric project ID (preferred). Get it from search_projects/search_units."},
            "project_name": {"type": "string", "description": "Project name if the ID isn't known (fuzzy-matched)."},
            "price": {"type": "number", "description": "Price in AED. Optional for a project (defaults to its entry price); required for a hypothetical with no project."},
            "beds": {"type": "number", "description": "Bedrooms, to refine net yield. Optional."},
            "sqft": {"type": "number", "description": "Unit size in sqft, to refine net yield & price/sqft. Optional."},
            "community": {"type": "string", "description": "Community/district for the area model, e.g. 'Dubai Marina'. Optional for a project (uses its district)."},
            "is_rent": {"type": "boolean", "description": "Use rental days-on-market for Time-to-Sell instead of sale. Default false.", "default": False},
        },
        "required": [],
    },
    handler=get_investment_metrics_handler,
))
