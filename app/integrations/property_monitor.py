"""Property Monitor AVM API client (https://api.propertymonitor.com).

Provides automated valuations (AVM) plus real market / rental / yield data for the
UAE — the data that lets us value a property against its estimated worth and report
OBSERVED rental yields instead of estimates.

Auth: two header keys (X-API-KEY + COMPANY-KEY), set via PM_API_KEY / PM_COMPANY_KEY.
Until those are configured, `configured()` is False and tools should return a clear
"not configured" message rather than calling the API.

NOTE on the consumer flow: a full report (consumer-avm, market-trends, comparables)
is gated behind an OTP sent to an email/phone via POST /generate. That suits an
agent-triggered, on-demand valuation. Whether COMPANY-KEY bypasses the OTP for
server-side bulk enrichment is an open question to confirm with Property Monitor
(see plan §7); the methods below cover both the lookups and the report flow.
"""
import logging
from typing import Any, Optional
import httpx
from app.config import settings

log = logging.getLogger("askalpha.property_monitor")


class PropertyMonitorError(Exception):
    pass


def configured() -> bool:
    return bool(settings.pm_api_key and settings.pm_company_key)


def _headers() -> dict:
    return {
        "X-API-KEY": settings.pm_api_key,
        "COMPANY-KEY": settings.pm_company_key,
        "Accept": "application/json",
    }


async def _get(path: str, params: dict | None = None) -> Any:
    if not configured():
        raise PropertyMonitorError("Property Monitor API keys are not configured.")
    url = f"{settings.pm_base_url}{path}"
    async with httpx.AsyncClient(timeout=40.0, headers=_headers(), http2=True) as client:
        resp = await client.get(url, params={k: v for k, v in (params or {}).items() if v is not None})
        if resp.status_code >= 400:
            raise PropertyMonitorError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()


async def _post(path: str, body: dict) -> Any:
    if not configured():
        raise PropertyMonitorError("Property Monitor API keys are not configured.")
    url = f"{settings.pm_base_url}{path}"
    async with httpx.AsyncClient(timeout=40.0, headers=_headers(), http2=True) as client:
        resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            raise PropertyMonitorError(f"POST {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()


# ---- Lookups (no report/OTP needed) ----
async def search_locations(q: str, emirate_id: str = "4", area_name: str | None = None,
                           master_development: str | None = None, sub_loc1: str | None = None) -> Any:
    """Resolve an area name to Property Monitor location(s). Optional area_name/masterDevelopment/
    subLoc1 narrow the match (Dubai emirateId=4)."""
    return await _get("/pm/v1/avm/locations", {
        "q": q, "emirateId": emirate_id, "areaName": area_name,
        "masterDevelopment": master_development, "subLoc1": sub_loc1,
    })


async def property_search(location_id: str | None = None, q: str | None = None, emirate_id: str = "4") -> Any:
    return await _get("/pm/v1/avm/property-search", {"locationId": location_id, "q": q, "emirateId": emirate_id})


# ---- AVM report flow ----
async def create_avm(location_id: int, unit_size_sqft: float, property_type_id: int,
                     bedrooms: str, views: str = "", upgrades: str = "") -> Any:
    """Pre-flight create a consumer AVM. Returns a payload that includes a reportHash."""
    return await _post("/pm/v1/avm/pre-flight-run-consumer-avm", {
        "locationId": location_id, "unitSizeSqft": unit_size_sqft,
        "propertyTypeId": property_type_id, "bedrooms": bedrooms,
        "views": views, "upgrades": upgrades,
    })


async def generate_otp(report_hash: str, email: str = "", phone: str = "") -> Any:
    """Trigger the OTP needed to unlock a report's data."""
    return await _post("/pm/v1/avm/generate", {"reportHash": report_hash, "email": email, "phone": phone})


async def get_avm_result(report_hash: str) -> Any:
    """The Preview/Consumer AVM report by hash — full data (valuation, ppsf, comps, service
    charges). With our COMPANY-KEY this returns without an OTP."""
    return await _get("/pm/v1/avm/consumer-avm", {"reportHash": report_hash})


async def get_market_trends(report_hash: str, data_type: str | None = None) -> Any:
    """PMDPI market trends (sales/rentals/yields). Omitting data_type returns the full set."""
    return await _get("/pm/v1/avm/market-trends", {"reportHash": report_hash, "dataType": data_type})


async def get_comparables(report_hash: str, evidence_type: str = "TRA") -> Any:
    """Local market activity. evidence_type: ACT (active listings) | TRA (transferred sales)."""
    return await _get("/pm/v1/avm/local-market-activity", {
        "reportHash": report_hash, "evidenceType": evidence_type,
    })


async def get_lowest_highest(report_hash: str) -> Any:
    """Lowest & highest comparable transactions for the report's location."""
    return await _get("/pm/v1/avm/lowest-highest-price-transaction", {"reportHash": report_hash})


async def get_sold_properties(report_hash: str) -> Any:
    """Recently sold/transferred properties for the report's location."""
    return await _get("/pm/v1/avm/sold-properties", {"reportHash": report_hash})


async def get_about_location(report_hash: str) -> Any:
    """About-the-location narrative / descriptive payload."""
    return await _get("/pm/v1/avm/about-the-location", {"reportHash": report_hash})
