import json
import math
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project
from app.integrations import overpass
from app.tools.registry import Tool, registry


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


async def _populate_from_overpass(db: AsyncSession, project: Project, radius_m: int) -> None:
    """Lazy-fill the POI cache for one project from OSM and compute distances."""
    pois = await overpass.fetch_pois(project.lat, project.lng, radius_m)
    for poi in pois:
        row = (await db.execute(
            text("""
                INSERT INTO points_of_interest (name, category, lat, lng, source, source_ref, raw)
                VALUES (:name, :cat, :lat, :lng, 'osm', :ref, CAST(:raw AS jsonb))
                ON CONFLICT (source, source_ref) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
            """),
            {"name": poi["name"], "cat": poi["category"], "lat": poi["lat"], "lng": poi["lng"],
             "ref": poi["source_ref"], "raw": json.dumps(poi["raw"])},
        )).first()
        poi_id = row[0]
        dist = _haversine_m(project.lat, project.lng, poi["lat"], poi["lng"])
        await db.execute(
            text("""
                INSERT INTO project_pois (project_id, poi_id, category, distance_m)
                VALUES (:pid, :poi_id, :cat, :dist)
                ON CONFLICT (project_id, poi_id) DO UPDATE SET distance_m = EXCLUDED.distance_m
            """),
            {"pid": project.id, "poi_id": poi_id, "cat": poi["category"], "dist": round(dist, 1)},
        )
    await db.commit()


async def get_nearby_amenities_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id is required"}
    category = (args.get("category") or "").strip().lower() or None
    radius_m = int(args.get("radius_m", 3000))
    per_category = min(int(args.get("per_category", 3)), 10)

    project = (await db.execute(select(Project).where(Project.id == int(project_id)))).scalar_one_or_none()
    if project is None:
        return {"error": f"No project found with id {project_id}"}
    if project.lat is None or project.lng is None:
        return {"found": False, "message": "This project has no location on record."}

    # Use the cache; if empty for this project, lazily fetch from OSM once.
    cached = (await db.execute(
        text("SELECT count(*) FROM project_pois WHERE project_id = :pid"), {"pid": project.id}
    )).scalar_one()
    if not cached:
        await _populate_from_overpass(db, project, max(radius_m, 3000))

    rows = (await db.execute(
        text("""
            SELECT l.category, p.name, p.lat, p.lng, l.distance_m
            FROM project_pois l
            JOIN points_of_interest p ON p.id = l.poi_id
            WHERE l.project_id = :pid AND l.distance_m <= :radius
              AND (CAST(:cat AS text) IS NULL OR l.category = :cat)
            ORDER BY l.category, l.distance_m
        """),
        {"pid": project.id, "radius": radius_m, "cat": category},
    )).mappings().all()

    by_cat: dict[str, list] = {}
    for r in rows:
        bucket = by_cat.setdefault(r["category"], [])
        if len(bucket) < per_category:
            bucket.append({
                "name": r["name"] or "(unnamed)",
                "distance_m": round(float(r["distance_m"])),
                "lat": r["lat"], "lng": r["lng"],
            })

    return {
        "found": True,
        "project_id": project.id,
        "project_name": project.name,
        "radius_m": radius_m,
        "categories": by_cat,
        "total": sum(len(v) for v in by_cat.values()),
        "source": "OpenStreetMap",
    }


registry.register(Tool(
    name="get_nearby_amenities",
    description=(
        "List amenities near a project — schools, hospitals, clinics, pharmacies, malls, "
        "supermarkets, metro stations, parks, beaches — with distances, grouped by category. "
        "Use when the user asks what's near a project, e.g. 'what schools are near X', "
        "'is there a hospital close to Y', 'how far is the metro'. Provide the project_id "
        "(get it from search_projects/search_units first). Optionally filter by category."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Numeric project ID."},
            "category": {
                "type": "string",
                "enum": ["school", "hospital", "clinic", "pharmacy", "mall", "supermarket", "metro", "park", "beach"],
                "description": "Optional: restrict to one amenity category.",
            },
            "radius_m": {"type": "integer", "description": "Search radius in metres (default 3000).", "default": 3000},
            "per_category": {"type": "integer", "description": "Max results per category (default 3, max 10).", "default": 3},
        },
        "required": ["project_id"],
    },
    handler=get_nearby_amenities_handler,
))
