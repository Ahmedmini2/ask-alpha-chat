from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Developer
from app.tools.registry import Tool, registry


async def _resolve_developer(db: AsyncSession, args: dict):
    did = args.get("developer_id")
    if did is not None:
        return (await db.execute(select(Developer).where(Developer.id == int(did)))).scalar_one_or_none()
    name = (args.get("developer_name") or "").strip()
    if not name:
        return None
    d = (await db.execute(
        select(Developer).where(Developer.name.ilike(f"%{name}%")).limit(1)
    )).scalar_one_or_none()
    if d:
        return d
    sim = func.similarity(Developer.name, name)
    return (await db.execute(
        select(Developer).where(sim > 0.4).order_by(sim.desc()).limit(1)
    )).scalar_one_or_none()


async def get_developer_profile_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    d = await _resolve_developer(db, args)
    if d is None:
        return {"found": False, "message": "We don't have that developer in our system yet."}

    stats = (await db.execute(
        text("""
            SELECT
              count(*)                                                        AS total_projects,
              count(*) FILTER (WHERE sale_status ILIKE '%sale%')              AS on_sale,
              count(*) FILTER (WHERE completion_date < now())                 AS delivered,
              count(*) FILTER (WHERE completion_date >= now())                AS upcoming,
              count(*) FILTER (WHERE completion_date IS NOT NULL
                               AND construction_end_date IS NOT NULL
                               AND completion_date <= construction_end_date)  AS on_time,
              count(*) FILTER (WHERE completion_date IS NOT NULL
                               AND construction_end_date IS NOT NULL)         AS with_dates,
              count(DISTINCT district)                                        AS districts,
              count(DISTINCT city)                                            AS cities,
              min(min_price) FILTER (WHERE min_price > 0)                     AS cheapest_from,
              max(max_price)                                                  AS priciest_to
            FROM projects WHERE developer_id = :id AND is_published = true
        """),
        {"id": d.id},
    )).mappings().first()

    notable = (await db.execute(
        text("""
            SELECT id, name, district, sale_status, completion_quarter, min_price
            FROM projects
            WHERE developer_id = :id AND is_published = true
            ORDER BY max_price DESC NULLS LAST
            LIMIT 5
        """),
        {"id": d.id},
    )).mappings().all()

    on_time_rate = None
    if stats and stats["with_dates"]:
        on_time_rate = round(100.0 * stats["on_time"] / stats["with_dates"], 0)

    return {
        "found": True,
        "developer_id": d.id,
        "name": d.name,
        "website": d.website,
        "description": d.description,
        "logo_s3_url": d.logo_s3_url,
        "total_projects": stats["total_projects"] if stats else 0,
        "on_sale": stats["on_sale"] if stats else 0,
        "delivered": stats["delivered"] if stats else 0,
        "upcoming": stats["upcoming"] if stats else 0,
        "on_time_delivery_pct": on_time_rate,
        "on_time_basis": (f"{stats['on_time']}/{stats['with_dates']} dated projects" if stats and stats["with_dates"] else "no delivery dates on record"),
        "districts_active": stats["districts"] if stats else 0,
        "cities_active": stats["cities"] if stats else 0,
        "price_range_aed": {
            "from": float(stats["cheapest_from"]) if stats and stats["cheapest_from"] else None,
            "to": float(stats["priciest_to"]) if stats and stats["priciest_to"] else None,
        },
        "notable_projects": [
            {
                "id": n["id"], "name": n["name"], "district": n["district"],
                "sale_status": n["sale_status"], "completion_quarter": n["completion_quarter"],
                "min_price": float(n["min_price"]) if n["min_price"] else None,
            } for n in notable
        ],
    }


registry.register(Tool(
    name="get_developer_profile",
    description=(
        "Get a developer's track record: total projects, how many are on sale / delivered / "
        "upcoming, an on-time delivery rate (where dates exist), how many districts and cities "
        "they operate in, their price range, and their notable projects. Use when the user asks "
        "about a developer, their reputation, portfolio, or reliability. Identify by developer_name "
        "or developer_id. If found=false we don't have that developer."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "developer_name": {"type": "string", "description": "Developer name, e.g. 'Emaar', 'DAMAC', 'Sobha'."},
            "developer_id": {"type": "integer", "description": "Numeric developer ID, if known."},
        },
        "required": [],
    },
    handler=get_developer_profile_handler,
))
