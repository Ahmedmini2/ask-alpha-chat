import re
from typing import Optional
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project
from app.tools.projects import _serialize_project_summary
from app.tools.registry import Tool, registry


def _alnum(s: str) -> str:
    """Lowercase, strip every non-alphanumeric char. Makes matching robust to the
    odd whitespace in some district strings (e.g. a non-breaking space inside
    'Downtown Dubai' that defeats a plain ILIKE '%Downtown Dubai%')."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

# Great-circle distance in km between two lat/lng points, as a raw SQL expression.
# Pure trig — no PostGIS/earthdistance extension needed (fast over ~1.9k rows).
_HAVERSINE = (
    "6371 * 2 * asin(sqrt("
    "power(sin(radians(p.lat - :clat)/2), 2) + "
    "cos(radians(:clat)) * cos(radians(p.lat)) * "
    "power(sin(radians(p.lng - :clng)/2), 2)))"
)


async def _resolve_anchor(db: AsyncSession, area: str) -> Optional[dict]:
    """Resolve a named area to a centroid by averaging the lat/lng of its projects.
    Tries district first (most specific), then region, then city. Matching is
    whitespace/punctuation-insensitive (see _alnum)."""
    pat = f"%{_alnum(area)}%"
    if pat == "%%":
        return None
    for col in ("district", "region", "city"):
        row = (await db.execute(
            text(f"""
                SELECT avg(lat) AS clat, avg(lng) AS clng, count(*) AS n
                FROM projects
                WHERE lat IS NOT NULL AND lng IS NOT NULL
                  AND regexp_replace(lower({col}), '[^a-z0-9]', '', 'g') LIKE :pat
            """),
            {"pat": pat},
        )).mappings().first()
        if row and row["n"] and row["clat"] is not None:
            return {"clat": float(row["clat"]), "clng": float(row["clng"]), "n": int(row["n"])}
    return None


async def search_nearby_projects_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    lat = args.get("lat")
    lng = args.get("lng")
    area = (args.get("area") or "").strip()
    radius_km = min(float(args.get("radius_km", 5)), 25.0)
    limit = min(int(args.get("limit", 5)), 5)
    offset = max(int(args.get("offset", 0)), 0)

    if lat is not None and lng is not None:
        clat, clng = float(lat), float(lng)
        anchor_label = f"({clat:.4f}, {clng:.4f})"
    elif area:
        anchor = await _resolve_anchor(db, area)
        if anchor is None:
            return {"found": False, "error": f"Could not locate '{area}' to search around.", "area": area}
        clat, clng = anchor["clat"], anchor["clng"]
        anchor_label = area
    else:
        return {"error": "Provide either an 'area' name or explicit 'lat' and 'lng'."}

    params = {"clat": clat, "clng": clng, "radius": radius_km, "lim": limit + 1, "off": offset}
    rows = (await db.execute(
        text(f"""
            SELECT p.id, {_HAVERSINE} AS distance_km
            FROM projects p
            WHERE p.is_published = true AND p.lat IS NOT NULL AND p.lng IS NOT NULL
              AND {_HAVERSINE} <= :radius
            ORDER BY distance_km ASC
            OFFSET :off LIMIT :lim
        """),
        params,
    )).mappings().all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return {
            "found": True, "count": 0, "has_more": False, "next_offset": None,
            "anchor": anchor_label, "radius_km": radius_km, "projects": [],
        }

    # Fetch the Project ORM rows (with developer) and re-attach distance in order.
    ids = [r["id"] for r in rows]
    dist_by_id = {r["id"]: round(float(r["distance_km"]), 2) for r in rows}
    objs = (await db.execute(select(Project).where(Project.id.in_(ids)))).scalars().all()
    by_id = {o.id: o for o in objs}

    projects = []
    for r in rows:  # preserve distance order
        p = by_id.get(r["id"])
        if p is None:
            continue
        summary = _serialize_project_summary(p)
        summary["distance_km"] = dist_by_id[r["id"]]
        projects.append(summary)

    return {
        "found": True,
        "count": len(projects),
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
        "anchor": anchor_label,
        "radius_km": radius_km,
        "projects": projects,
    }


registry.register(Tool(
    name="search_nearby_projects",
    description=(
        "Find projects geographically near a place or coordinate, sorted by distance. "
        "Use this for proximity queries like 'projects within 5km of Downtown', "
        "'developments near Palm Jumeirah', or 'what's close to the marina'. "
        "Anchor by an 'area' name (resolved to the centre of that area's projects) OR by "
        "explicit lat/lng. Each result includes distance_km. radius_km defaults to 5, max 25. "
        "Returns found=false if the area can't be located."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "description": "Place to search around, e.g. 'Downtown Dubai', 'Palm Jumeirah', 'Dubai Marina'. Either this or lat+lng is required.",
            },
            "lat": {"type": "number", "description": "Anchor latitude (use with lng instead of area)."},
            "lng": {"type": "number", "description": "Anchor longitude (use with lat instead of area)."},
            "radius_km": {
                "type": "number",
                "description": "Search radius in kilometres. Default 5, maximum 25.",
                "default": 5,
            },
            "limit": {"type": "integer", "description": "Max results per page (default 5, max 5).", "default": 5},
            "offset": {"type": "integer", "description": "Pagination offset (use next_offset for the next page).", "default": 0},
        },
        "required": [],
    },
    handler=search_nearby_projects_handler,
))
