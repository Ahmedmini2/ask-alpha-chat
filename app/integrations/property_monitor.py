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
    async with httpx.AsyncClient(timeout=40.0, headers=_headers()) as client:
        resp = await client.get(url, params={k: v for k, v in (params or {}).items() if v is not None})
        if resp.status_code >= 400:
            raise PropertyMonitorError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()


async def _post(path: str, body: dict) -> Any:
    if not configured():
        raise PropertyMonitorError("Property Monitor API keys are not configured.")
    url = f"{settings.pm_base_url}{path}"
    async with httpx.AsyncClient(timeout=40.0, headers=_headers()) as client:
        resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            raise PropertyMonitorError(f"POST {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()


# ---- Lookups (no report/OTP needed) ----
async def search_locations(q: str, emirate_id: str = "4") -> Any:
    """Resolve an area name to a Property Monitor locationId (Dubai=4)."""
    return await _get("/pm/v1/avm/locations", {"q": q, "emirateId": emirate_id})


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


async def get_avm_result(report_hash: str, otp_code: str | None = None) -> Any:
    return await _get("/pm/v1/avm/consumer-avm", {"reportHash": report_hash, "otpCode": otp_code})


async def get_market_trends(report_hash: str, data_type: str = "yields",
                            otp_code: str | None = None, emirate_id: str | None = None) -> Any:
    """PMDPI market trends. data_type one of: sales | rentals | yields."""
    return await _get("/pm/v1/avm/market-trends", {
        "reportHash": report_hash, "dataType": data_type, "otpCode": otp_code, "emirateId": emirate_id,
    })


async def get_comparables(report_hash: str, evidence_type: str = "TRA", otp_code: str | None = None) -> Any:
    """Local market activity. evidence_type: ACT (active listings) | TRA (transferred sales)."""
    return await _get("/pm/v1/avm/local-market-activity", {
        "reportHash": report_hash, "evidenceType": evidence_type, "otpCode": otp_code,
    })
