"""Property Monitor valuation tool.

Surfaces AVM valuations to Ask Alpha. Until PM_API_KEY / PM_COMPANY_KEY are set,
the handler returns a clear "not configured" message so the assistant can tell the
agent what's needed rather than erroring. See app/integrations/property_monitor.py
and plan §7 (the consumer report flow is OTP-gated)."""
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.analytics.alpha_verdict import canonical_community_slug
from app.integrations import property_monitor as pm
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.pm_tools")


async def get_property_valuation_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    if not pm.configured():
        return {
            "configured": False,
            "message": (
                "Property Monitor valuations aren't enabled yet. Once the PM_API_KEY and "
                "PM_COMPANY_KEY are configured, I can value a property against the market "
                "using the AVM and report observed rental yields."
            ),
        }

    area = (args.get("area") or "").strip()
    size_sqft = args.get("unit_size_sqft")
    bedrooms = str(args.get("bedrooms", "")).strip()
    property_type_id = args.get("property_type_id")
    if not area or size_sqft is None or property_type_id is None:
        return {"error": "area, unit_size_sqft, and property_type_id are required"}

    try:
        locs = await pm.search_locations(area)
        loc_list = locs if isinstance(locs, list) else (locs.get("data") or locs.get("results") or [])
        if not loc_list:
            return {"found": False, "message": f"Property Monitor has no location matching '{area}'."}
        location_id = (loc_list[0].get("locationId") or loc_list[0].get("id"))

        avm = await pm.create_avm(
            location_id=int(location_id), unit_size_sqft=float(size_sqft),
            property_type_id=int(property_type_id), bedrooms=bedrooms,
        )
        report_hash = avm.get("reportHash") or (avm.get("data") or {}).get("reportHash")
        return {
            "found": True,
            "location_id": location_id,
            "report_hash": report_hash,
            "avm": avm,
            "note": (
                "Created an AVM report. Full valuation, comparables and yields may require an "
                "OTP (sent to the agent's email/phone) to unlock — confirm the PM access tier."
            ),
        }
    except pm.PropertyMonitorError as e:
        log.warning("Property Monitor valuation failed: %s", e)
        return {"error": f"Property Monitor request failed: {e}"}


registry.register(Tool(
    name="get_property_valuation",
    description=(
        "Get a Property Monitor automated valuation (AVM) for a specific property in Dubai, to "
        "compare an asking price against the estimated market value and (where available) observed "
        "rental yields. Provide the area, unit size in sqft, bedrooms, and property_type_id. Use this "
        "when the user asks what a property is really worth or whether an asking price is fair. If it "
        "reports configured=false, tell the user Property Monitor isn't enabled yet."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "area": {"type": "string", "description": "Area / community name, e.g. 'Dubai Marina', 'Palm Jumeirah'."},
            "unit_size_sqft": {"type": "number", "description": "Unit size in square feet."},
            "bedrooms": {"type": "string", "description": "Bedrooms, e.g. '2', '3', 'studio'."},
            "property_type_id": {"type": "integer", "description": "Property Monitor property type id (apartment/villa/etc.)."},
        },
        "required": ["area", "unit_size_sqft", "property_type_id"],
    },
    handler=get_property_valuation_handler,
))


async def get_live_market_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    """Return stored Property Monitor live-market data (AVM valuation, ppsf, observed yield, recent
    sold comps) for a project or area, from the pm_* tables. Distinct from the Alpha Verdict's area
    model. available=False until the PM ingest has populated that community."""
    # Resolve a canonical community slug from a project or a bare area name.
    community_name = (args.get("area") or "").strip()
    project_id = args.get("project_id")
    project_name = (args.get("project_name") or "").strip()
    pname = None
    if (project_id is not None or project_name) and not community_name:
        from app.tools.investment_metrics import _resolve_project
        proj = await _resolve_project(db, {"project_id": project_id, "project_name": project_name})
        if proj is not None:
            community_name = proj.district or proj.city or ""
            pname = proj.name
            project_id = proj.id
    if not community_name:
        return {"available": False, "message": "Give me a project or an area for live market data."}

    slug = canonical_community_slug(community_name)
    stats = (await db.execute(text(
        "SELECT community_label, gross_yield, appreciation, ppsf_aed, service_charge_aed_sqft, updated_at "
        "FROM pm_community_stats WHERE community_slug=:s"), {"s": slug})).mappings().first()
    rep = (await db.execute(text(
        "SELECT valuation_aed, valuation_low_aed, valuation_high_aed, ppsf_aed, "
        "annual_service_charge_aed, confidence_level, fetched_at FROM pm_reports "
        "WHERE community_slug=:s AND project_id IS NULL ORDER BY fetched_at DESC LIMIT 1"),
        {"s": slug})).mappings().first()
    sold = (await db.execute(text(
        "SELECT raw, fetched_at FROM pm_sold WHERE community_slug=:s"), {"s": slug})).mappings().first()

    if not stats and not rep:
        return {"available": False, "community": community_name, "project_name": pname,
                "message": (f"Property Monitor data for {community_name} isn't loaded yet. "
                            "It's ingested per community; ask an admin to run the PM ingest.")}

    def _f(x):
        return float(x) if x is not None else None

    return {
        "available": True,
        "source": "Property Monitor (live)",
        "project_id": project_id,
        "project_name": pname,
        "community": (stats or {}).get("community_label") or community_name,
        "valuation": _f((rep or {}).get("valuation_aed")),
        "valuation_range": [_f((rep or {}).get("valuation_low_aed")), _f((rep or {}).get("valuation_high_aed"))],
        "ppsf_aed": _f((rep or stats or {}).get("ppsf_aed")),
        "observed_yield_pct": (round(_f(stats["gross_yield"]) * 100, 1) if stats and stats["gross_yield"] else None),
        "appreciation_pct": (round(_f(stats["appreciation"]) * 100, 1) if stats and stats["appreciation"] else None),
        "annual_service_charge_aed": _f((rep or {}).get("annual_service_charge_aed")),
        "confidence": (rep or {}).get("confidence_level"),
        "sold": (sold or {}).get("raw"),
        "fetched_at": str((rep or stats or {}).get("fetched_at") or (stats or {}).get("updated_at") or ""),
    }


registry.register(Tool(
    name="get_live_market",
    description=(
        "Get LIVE Property Monitor market data for a project or area — the real AVM valuation, "
        "price/sqft, observed rental yield, appreciation, and recent SOLD comparables, from our "
        "ingested Property Monitor tables. Use this when the user wants real/live numbers, an actual "
        "valuation, 'what did units actually sell for', or observed (not modelled) yields. This is "
        "third-party live data — distinct from get_alpha_verdict (our area model). Identify by "
        "project_id/project_name or a bare area. If available=false, the PM data for that community "
        "isn't loaded yet."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Numeric project ID (preferred)."},
            "project_name": {"type": "string", "description": "Project name (fuzzy-matched)."},
            "area": {"type": "string", "description": "Area/community name, e.g. 'Dubai Marina'. Use instead of a project."},
        },
        "required": [],
    },
    handler=get_live_market_handler,
))
