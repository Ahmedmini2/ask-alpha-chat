from typing import Any
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project, ProjectAlphaVerdict, ProjectUnit
from app.tools.projects import _serialize_project_summary
from app.tools.registry import Tool, registry


# Canonical unit_type values in the DB are lowercase and plural:
# apartments / villa / townhouse / duplex / penthouse / 'hotel apartments'.
# Map common user synonyms onto them so the LLM (and users) can say "apartment",
# "flat", "villas", etc. Anything already canonical passes through unchanged.
_UNIT_TYPE_SYNONYMS = {
    "apartment": "apartments",
    "apartments": "apartments",
    "flat": "apartments",
    "flats": "apartments",
    "villa": "villa",
    "villas": "villa",
    "townhouse": "townhouse",
    "townhouses": "townhouse",
    "town house": "townhouse",
    "duplex": "duplex",
    "duplexes": "duplex",
    "penthouse": "penthouse",
    "penthouses": "penthouse",
    "hotel apartment": "hotel apartments",
    "hotel apartments": "hotel apartments",
}


def _normalize_unit_types(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for t in raw:
        key = str(t).strip().lower()
        mapped = _UNIT_TYPE_SYNONYMS.get(key, key)
        if mapped not in out:
            out.append(mapped)
    return out


async def search_units_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    """Search projects by the attributes of their UNITS (bedrooms, unit type, size,
    per-unit price). search_projects only filters project-level columns and can't
    answer "4BR villa under 10M" — this joins project_units, filters at the unit
    level, and aggregates the matching units back up to the project."""
    unit_types = _normalize_unit_types(args.get("unit_type"))
    bedrooms_min = args.get("bedrooms_min")
    bedrooms_max = args.get("bedrooms_max")
    min_unit_price = args.get("min_unit_price")
    max_unit_price = args.get("max_unit_price")
    min_size = args.get("min_size")
    max_size = args.get("max_size")
    location = args.get("location")
    limit = min(int(args.get("limit", 5)), 5)
    offset = max(int(args.get("offset", 0)), 0)

    price_filter_applied = min_unit_price is not None or max_unit_price is not None

    # ---- Build the per-project aggregate over matching units. ----
    unit_filters = []
    if unit_types:
        unit_filters.append(func.lower(ProjectUnit.unit_type).in_(unit_types))
    if bedrooms_min is not None:
        unit_filters.append(ProjectUnit.bedrooms >= float(bedrooms_min))
    if bedrooms_max is not None:
        unit_filters.append(ProjectUnit.bedrooms <= float(bedrooms_max))
    if min_size is not None:
        unit_filters.append(ProjectUnit.size >= float(min_size))
    if max_size is not None:
        unit_filters.append(ProjectUnit.size <= float(max_size))
    # When the user gives a price band, only count priced units in that band — a
    # zero/NULL price is missing data, not a free unit (mirrors projects.py).
    if price_filter_applied:
        unit_filters.append(ProjectUnit.price > 0)
        if min_unit_price is not None:
            unit_filters.append(ProjectUnit.price >= float(min_unit_price))
        if max_unit_price is not None:
            unit_filters.append(ProjectUnit.price <= float(max_unit_price))

    agg = (
        select(
            ProjectUnit.project_id.label("project_id"),
            func.min(ProjectUnit.price).filter(ProjectUnit.price > 0).label("unit_min_price"),
            func.max(ProjectUnit.price).filter(ProjectUnit.price > 0).label("unit_max_price"),
            func.min(ProjectUnit.bedrooms).label("bed_min"),
            func.max(ProjectUnit.bedrooms).label("bed_max"),
            func.min(ProjectUnit.size).filter(ProjectUnit.size > 0).label("size_min"),
            func.max(ProjectUnit.size).filter(ProjectUnit.size > 0).label("size_max"),
            func.count().label("matched_units"),
            func.array_agg(func.distinct(func.lower(ProjectUnit.unit_type))).label("unit_types"),
        )
        .where(*unit_filters)
        .group_by(ProjectUnit.project_id)
        .subquery()
    )

    stmt = select(
        Project,
        agg.c.unit_min_price,
        agg.c.unit_max_price,
        agg.c.bed_min,
        agg.c.bed_max,
        agg.c.size_min,
        agg.c.size_max,
        agg.c.matched_units,
        agg.c.unit_types,
    ).join(agg, Project.id == agg.c.project_id).where(
        Project.is_published == True  # noqa: E712
    )

    if location:
        loc = f"%{location}%"
        stmt = stmt.where(or_(
            Project.city.ilike(loc),
            Project.region.ilike(loc),
            Project.district.ilike(loc),
            Project.country.ilike(loc),
        ))

    # A price band filters at the unit level above; here we keep only projects that
    # actually have a matching priced unit so the project_min reflects real data.
    if price_filter_applied:
        stmt = stmt.where(agg.c.unit_min_price.is_not(None))

    # CONVICTION-FIRST RANKING (standing product rule): lead with the highest Alpha Verdict
    # conviction, price ascending (the matching units' entry price) as the tiebreaker — server-side.
    # LEFT JOIN so unscored projects still appear last (NULLS LAST).
    stmt = (stmt.outerjoin(ProjectAlphaVerdict, ProjectAlphaVerdict.project_id == Project.id)
                .order_by(ProjectAlphaVerdict.conviction.desc().nullslast(),
                          agg.c.unit_min_price.asc().nulls_last(),
                          Project.id))

    # Fetch limit+1 to detect has_more without a separate COUNT.
    stmt = stmt.offset(offset).limit(limit + 1)
    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    vmap: dict[int, tuple] = {}
    if rows:
        vrows = (await db.execute(
            select(ProjectAlphaVerdict.project_id, ProjectAlphaVerdict.verdict,
                   ProjectAlphaVerdict.conviction)
            .where(ProjectAlphaVerdict.project_id.in_([r[0].id for r in rows]))
        )).all()
        vmap = {pid: (verd, float(conv)) for pid, verd, conv in vrows}

    projects: list[dict] = []
    for row in rows:
        p = row[0]
        summary = _serialize_project_summary(p)
        _vc = vmap.get(p.id)
        summary["verdict"] = _vc[0] if _vc else None
        summary["conviction"] = round(_vc[1]) if _vc else None
        summary["matched_units"] = {
            "count": int(row.matched_units) if row.matched_units is not None else 0,
            "min_price": float(row.unit_min_price) if row.unit_min_price is not None else None,
            "max_price": float(row.unit_max_price) if row.unit_max_price is not None else None,
            "bedrooms_min": float(row.bed_min) if row.bed_min is not None else None,
            "bedrooms_max": float(row.bed_max) if row.bed_max is not None else None,
            "size_min": float(row.size_min) if row.size_min is not None else None,
            "size_max": float(row.size_max) if row.size_max is not None else None,
            "unit_types": [t for t in (row.unit_types or []) if t],
            "currency": p.currency,
        }
        projects.append(summary)

    return {
        "count": len(projects),
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
        "projects": projects,
        "filters": {
            "unit_type": unit_types or None,
            "bedrooms_min": bedrooms_min,
            "bedrooms_max": bedrooms_max,
            "min_unit_price": min_unit_price,
            "max_unit_price": max_unit_price,
            "min_size": min_size,
            "max_size": max_size,
            "location": location,
        },
    }


# ---------------------- Tool registration ----------------------

registry.register(Tool(
    name="search_units",
    description=(
        "Search projects by the attributes of their individual UNITS — bedrooms, unit type "
        "(apartment, villa, townhouse, duplex, penthouse), per-unit price, and size. "
        "Use this WHENEVER the user mentions any unit-level attribute, e.g. "
        "'4 bedroom villa under 10M', '2BR apartments between 1M and 2M in Dubai Marina', "
        "'penthouses over 2000 sqft'. Do NOT use search_projects for these — search_projects "
        "only filters project-level fields and cannot match bedrooms or unit type. "
        "Returns matching projects (same shape as search_projects) plus a 'matched_units' "
        "block per project describing the units that matched. Results come back ALREADY RANKED by "
        "Alpha Verdict conviction (highest first; the matching units' price ascending breaks ties), "
        "and each card shows its conviction score and BUY/WATCH/SKIP — present them in order; don't re-sort."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "unit_type": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["apartments", "villa", "townhouse", "duplex", "penthouse", "hotel apartments"],
                },
                "description": (
                    "One or more unit types to match (OR'd together). Map user words: "
                    "'apartment'/'flat' -> 'apartments', 'villas' -> 'villa', etc. "
                    "Example: ['villa','townhouse'] for 'villa or townhouse'."
                ),
            },
            "bedrooms_min": {
                "type": "integer",
                "description": "Minimum bedrooms. For an exact count like '4 bedroom', set bedrooms_min=4 AND bedrooms_max=4.",
            },
            "bedrooms_max": {
                "type": "integer",
                "description": "Maximum bedrooms. For an exact count, set equal to bedrooms_min.",
            },
            "min_unit_price": {
                "type": "number",
                "description": "Lower bound on the UNIT price in AED. Units priced 0/unknown are excluded when any price bound is set.",
            },
            "max_unit_price": {
                "type": "number",
                "description": "Upper bound on the UNIT price in AED, e.g. 'under 10M' -> 10000000.",
            },
            "min_size": {
                "type": "number",
                "description": "Minimum unit size in square feet (sqft).",
            },
            "max_size": {
                "type": "number",
                "description": "Maximum unit size in square feet (sqft).",
            },
            "location": {
                "type": "string",
                "description": "Optional location filter matched across city, region, district, country. E.g. 'Dubai Marina', 'Dubai'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results per page (default 5, max 5). Always paginate by 5.",
                "default": 5,
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset. Use next_offset from the previous result to fetch the next page.",
                "default": 0,
            },
        },
        "required": [],
    },
    handler=search_units_handler,
))
