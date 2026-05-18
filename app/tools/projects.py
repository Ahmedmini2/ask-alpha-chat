from typing import Any
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project, ProjectUnit
from app.tools.registry import Tool, registry


def _serialize_project_summary(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "developer": p.developer.name if p.developer else None,
        "city": p.city,
        "region": p.region,
        "district": p.district,
        "country": p.country,
        "sale_status": p.sale_status,
        "status": p.status,
        "completion_quarter": p.completion_quarter,
        "min_price": float(p.min_price) if p.min_price is not None else None,
        "max_price": float(p.max_price) if p.max_price is not None else None,
        "currency": p.currency,
        "short_description": p.short_description,
        "units_count": p.units_count,
    }


def _serialize_project_detail(p: Project) -> dict:
    base = _serialize_project_summary(p)
    base.update({
        "description": p.description,
        "amenities": p.amenities,
        "completion_date": p.completion_date.isoformat() if p.completion_date else None,
        "post_handover": p.post_handover,
        "has_escrow": p.has_escrow,
        "service_charge": p.service_charge,
        "furnishing": p.furnishing,
        "deposit_description": p.deposit_description,
        "managing_company": p.managing_company,
        "brand": p.brand,
        "marketing_brochure_url": p.marketing_brochure_url,
        "cover_image_url": p.cover_image_url,
        "units_summary": [
            {
                "unit_type": u.unit_type,
                "bedrooms": float(u.bedrooms) if u.bedrooms is not None else None,
                "bathrooms": float(u.bathrooms) if u.bathrooms is not None else None,
                "size": float(u.size) if u.size is not None else None,
                "price": float(u.price) if u.price is not None else None,
                "currency": u.currency,
                "area_unit": u.area_unit,
                "layout_name": u.layout_name,
                "status": u.status,
            }
            for u in p.units[:30]
        ],
    })
    return base


# ---------------------- Tool handlers ----------------------

async def search_projects_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = args.get("query")
    location = args.get("location")
    sale_status = args.get("sale_status")
    limit = min(int(args.get("limit", 5)), 5)
    offset = max(int(args.get("offset", 0)), 0)

    stmt = select(Project).where(Project.is_published == True)

    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(
            Project.name.ilike(like),
            Project.short_description.ilike(like),
            Project.description.ilike(like),
        ))
    if location:
        loc = f"%{location}%"
        stmt = stmt.where(or_(
            Project.city.ilike(loc),
            Project.region.ilike(loc),
            Project.district.ilike(loc),
            Project.country.ilike(loc),
        ))
    if sale_status:
        stmt = stmt.where(Project.sale_status.ilike(f"%{sale_status}%"))

    # Fetch limit+1 to detect has_more without a separate COUNT.
    stmt = stmt.order_by(Project.id).offset(offset).limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    projects = rows[:limit]
    return {
        "count": len(projects),
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
        "projects": [_serialize_project_summary(p) for p in projects],
    }


async def get_project_details_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id is required"}
    result = await db.execute(select(Project).where(Project.id == int(project_id)))
    p = result.scalar_one_or_none()
    if not p:
        return {"error": f"No project found with id {project_id}"}
    return _serialize_project_detail(p)


# ---------------------- Tool registrations ----------------------

registry.register(Tool(
    name="search_projects",
    description=(
        "Search the real estate project database by name, location, or sale status. "
        "Use this when the user mentions a project name or asks to find projects matching criteria. "
        "Returns matching projects with summary information."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text to match against project name and descriptions, e.g. 'Emaar Beachfront'",
            },
            "location": {
                "type": "string",
                "description": (
                    "Optional location filter. Matches across city, region, district, or country. "
                    "Examples: 'Dubai', 'Abu Dhabi', 'Dubai Marina', 'Downtown Dubai', 'UAE'"
                ),
            },
            "sale_status": {
                "type": "string",
                "description": "Optional sale status filter, e.g. 'On sale', 'Sold out'",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results per page (default 5, max 5). Always paginate by 5.",
                "default": 5,
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset. Use 0 for the first page, 5 for the second page, etc. When a previous call returned has_more=true, call again with offset=next_offset to fetch the next 5.",
                "default": 0,
            },
        },
        "required": [],
    },
    handler=search_projects_handler,
))

registry.register(Tool(
    name="get_project_details",
    description=(
        "Get full details of a specific project by ID, including pricing, units breakdown, "
        "amenities, payment terms, and brochure links. Use after search_projects when the user "
        "wants more depth on a specific project."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "integer",
                "description": "The numeric project ID",
            },
        },
        "required": ["project_id"],
    },
    handler=get_project_details_handler,
))
