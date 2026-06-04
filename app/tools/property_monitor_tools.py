"""Property Monitor valuation tool.

Surfaces AVM valuations to Ask Alpha. Until PM_API_KEY / PM_COMPANY_KEY are set,
the handler returns a clear "not configured" message so the assistant can tell the
agent what's needed rather than erroring. See app/integrations/property_monitor.py
and plan §7 (the consumer report flow is OTP-gated)."""
import logging
from sqlalchemy.ext.asyncio import AsyncSession
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
