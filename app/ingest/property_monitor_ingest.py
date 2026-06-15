"""Property Monitor ingestion — fills the pm_* tables with real market data per community we serve,
and derives pm_community_stats (yield / appreciation / ppsf / service charge) which FEEDS the Alpha
Verdict (replacing the static COMMUNITY_DATA constants where PM has data).

Per community: locations -> pre-flight AVM -> consumer-avm (preview) + market-trends + sold +
local-activity + lowest-highest + about-the-location. Raw payloads are stored as jsonb so the
derived scalars can be re-extracted/refined without re-calling PM.

Run (needs the caller IP allowlisted by Property Monitor):
    python -m app.ingest.property_monitor_ingest                # all communities
    python -m app.ingest.property_monitor_ingest 5              # first 5 communities (smoke)
    python -m app.ingest.property_monitor_ingest projects 100   # per-project AVM reports (opt-in)
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.analytics.alpha_verdict import canonical_community_slug, COMMUNITY_DATA
from app.config import settings
from app.db.models import PmCommunityStats
from app.db.session import AsyncSessionLocal
from app.integrations import property_monitor as pm

_json = json

log = logging.getLogger("askalpha.pm_ingest")


# --------------------------------------------------------------------------- extraction helpers

def _num(x) -> Optional[float]:
    try:
        return float(x) if x not in (None, "", 0, "0") else (0.0 if x in (0, "0") else None)
    except (TypeError, ValueError):
        return None


def _unwrap(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def _extract_avm(report: dict) -> dict:
    """Pull the known consumer-avm preview fields (shapes confirmed from PM samples)."""
    d = _unwrap(report) or {}
    ppsf = (d.get("pricePerSqftData") or {})
    return {
        "report_id": d.get("reportId"),
        "report_hash": d.get("reportHash"),
        "valuation_aed": _num(d.get("finalValuation")),
        "valuation_low_aed": _num(d.get("finalValuationLowerEnd")),
        "valuation_high_aed": _num(d.get("finalValuationHighEnd")),
        "ppsf_aed": _num(ppsf.get("indexedValue")),
        "service_charge_aed_sqft": _num(d.get("totalServiceCharges")),
        "annual_service_charge_aed": _num(d.get("annualServiceCharges")),
        "confidence_level": d.get("confidenceLevel"),
        "confidence_score": _num(d.get("finalConfidenceScore")),
    }


def _deep_find_number(obj: Any, key_substrings: tuple[str, ...]) -> Optional[float]:
    """Best-effort: walk a payload for the first numeric value whose key contains any substring.
    Used to pull yield / YoY-appreciation from market-trends until the exact shape is pinned on a
    live run."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(sub in kl for sub in key_substrings):
                n = _num(v)
                if n is not None:
                    return n
            found = _deep_find_number(v, key_substrings)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for it in obj:
            found = _deep_find_number(it, key_substrings)
            if found is not None:
                return found
    return None


def _extract_yield_appreciation(trends: Any) -> tuple[Optional[float], Optional[float]]:
    """Derive a community gross yield (decimal) and YoY appreciation (decimal) from market-trends.
    Best-effort + defensive; refine the key list after the first live payload is seen."""
    d = _unwrap(trends)
    y = _deep_find_number(d, ("grossyield", "gross_yield", "yield"))
    a = _deep_find_number(d, ("yoy", "appreciation", "growth", "annualchange", "change_pct"))
    # Normalize percentages (e.g. 6.2) to decimals (0.062).
    if y is not None and y > 1:
        y = y / 100.0
    if a is not None and abs(a) > 1:
        a = a / 100.0
    return y, a


# --------------------------------------------------------------------------- community ingest

async def _communities() -> list[tuple[str, str]]:
    """Distinct (canonical_slug, representative district name) among published projects."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT district FROM projects "
            "WHERE is_published AND district IS NOT NULL AND length(trim(district)) > 0"
        ))).all()
    seen: dict[str, str] = {}
    for (district,) in rows:
        slug = canonical_community_slug(district)
        seen.setdefault(slug, district)   # first district name per slug
    return sorted(seen.items())


def _pick_location(candidates: list[dict], community_name: str) -> Optional[dict]:
    if not candidates:
        return None
    cl = community_name.lower()
    apts = [c for c in candidates if (c.get("location_unit_type") or "").lower() == "apartment"]
    pool = apts or candidates
    # Prefer the master-development that matches the community name.
    for c in pool:
        if (c.get("master_development") or "").lower() == cl:
            return c
    return pool[0]


async def _upsert_raw(table: str, slug: str, report_hash: str, raw: Any) -> None:
    """Upsert a raw payload keyed by community_slug for the simple pm_* tables."""
    async with AsyncSessionLocal() as db:
        await db.execute(text(
            f"INSERT INTO {table} (community_slug, report_hash, raw, fetched_at) "
            "VALUES (:s, :h, cast(:r as jsonb), now()) "
            "ON CONFLICT (community_slug) DO UPDATE SET report_hash=excluded.report_hash, "
            "raw=excluded.raw, fetched_at=now()"
        ), {"s": slug, "h": report_hash, "r": json.dumps(raw)})
        await db.commit()


async def _ingest_one_community(slug: str, district: str) -> str:
    locs = _unwrap(await pm.search_locations(district, emirate_id=settings.pm_emirate_id)) or []
    loc = _pick_location(locs, district)
    if not loc:
        return "no-location"
    location_id = loc.get("location_id") or loc.get("id")
    async with AsyncSessionLocal() as db:
        await db.execute(text(
            "INSERT INTO pm_locations (community_slug, pm_location_id, pm_location_name, emirate_id, "
            "area_name, master_development, matched_query, raw, updated_at) VALUES "
            "(:s,:lid,:name,:em,:area,:master,:q, cast(:raw as jsonb), now()) "
            "ON CONFLICT (community_slug) DO UPDATE SET pm_location_id=excluded.pm_location_id, "
            "pm_location_name=excluded.pm_location_name, area_name=excluded.area_name, "
            "master_development=excluded.master_development, matched_query=excluded.matched_query, "
            "raw=excluded.raw, updated_at=now()"),
            {"s": slug, "lid": location_id, "name": loc.get("name"), "em": int(settings.pm_emirate_id),
             "area": loc.get("area_name"), "master": loc.get("master_development"), "q": district,
             "raw": _json.dumps(loc)})
        await db.commit()

    pre = _unwrap(await pm.create_avm(
        location_id=location_id, unit_size_sqft=settings.pm_default_size_sqft,
        property_type_id=settings.pm_default_property_type_id, bedrooms=settings.pm_default_bedrooms))
    report_hash = (pre or {}).get("reportHash")
    if not report_hash:
        return "no-hash"

    avm_raw = await pm.get_avm_result(report_hash)
    avm = _extract_avm(avm_raw)

    # store the community-level AVM report (project_id NULL); dedupe prior NULL-project rows.
    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM pm_reports WHERE community_slug=:s AND project_id IS NULL"),
                         {"s": slug})
        await db.execute(text(
            "INSERT INTO pm_reports (community_slug, pm_location_id, report_hash, report_id, bedrooms, "
            "unit_size_sqft, property_type_id, valuation_aed, valuation_low_aed, valuation_high_aed, "
            "ppsf_aed, service_charge_aed_sqft, annual_service_charge_aed, confidence_level, "
            "confidence_score, raw, fetched_at) VALUES "
            "(:s,:lid,:h,:rid,:beds,:size,:ptid,:val,:low,:high,:ppsf,:sc,:asc,:cl,:cs, cast(:raw as jsonb), now())"),
            {"s": slug, "lid": location_id, "h": report_hash, "rid": avm["report_id"],
             "beds": settings.pm_default_bedrooms, "size": settings.pm_default_size_sqft,
             "ptid": settings.pm_default_property_type_id, "val": avm["valuation_aed"],
             "low": avm["valuation_low_aed"], "high": avm["valuation_high_aed"], "ppsf": avm["ppsf_aed"],
             "sc": avm["service_charge_aed_sqft"], "asc": avm["annual_service_charge_aed"],
             "cl": avm["confidence_level"], "cs": avm["confidence_score"], "raw": json.dumps(_unwrap(avm_raw))})
        await db.commit()

    # secondary endpoints (best-effort; one failure doesn't abort the community).
    trends_raw = sold_raw = act_raw = lh_raw = about_raw = None
    for fn, table, var in [
        (pm.get_market_trends, "pm_market_trends", "trends"),
        (pm.get_sold_properties, "pm_sold", "sold"),
        (pm.get_comparables, "pm_local_activity", "act"),
        (pm.get_lowest_highest, "pm_lowest_highest", "lh"),
    ]:
        try:
            raw = await fn(report_hash)
            await _upsert_raw(table, slug, report_hash, _unwrap(raw))
            if var == "trends":
                trends_raw = raw
        except Exception as e:
            log.warning("%s for %s failed: %s", table, slug, e)
    try:
        about_raw = await pm.get_about_location(report_hash)
        async with AsyncSessionLocal() as db:
            await db.execute(text(
                "INSERT INTO pm_about_location (community_slug, report_hash, raw, fetched_at) "
                "VALUES (:s,:h, cast(:r as jsonb), now()) ON CONFLICT (community_slug) DO UPDATE SET "
                "report_hash=excluded.report_hash, raw=excluded.raw, fetched_at=now()"),
                {"s": slug, "h": report_hash, "r": json.dumps(_unwrap(about_raw))})
            await db.commit()
    except Exception as e:
        log.warning("about-location for %s failed: %s", slug, e)

    gross_yield, appreciation = _extract_yield_appreciation(trends_raw) if trends_raw else (None, None)
    label = COMMUNITY_DATA.get(slug, {}).get("label") or district
    async with AsyncSessionLocal() as db:
        stmt = pg_insert(PmCommunityStats).values(
            community_slug=slug, community_label=label, gross_yield=gross_yield,
            appreciation=appreciation, ppsf_aed=avm["ppsf_aed"],
            service_charge_aed_sqft=avm["service_charge_aed_sqft"], sample_n=None,
            updated_at=datetime.now(timezone.utc))
        stmt = stmt.on_conflict_do_update(
            index_elements=[PmCommunityStats.community_slug],
            set_={c: stmt.excluded[c] for c in ("community_label", "gross_yield", "appreciation",
                  "ppsf_aed", "service_charge_aed_sqft", "updated_at")})
        await db.execute(stmt)
        await db.commit()
    return "ok"


async def ingest_communities(limit: Optional[int] = None, concurrency: Optional[int] = None) -> dict:
    if not pm.configured():
        log.error("Property Monitor not configured (PM_API_KEY / PM_COMPANY_KEY).")
        return {"error": "not_configured"}
    comms = await _communities()
    if limit:
        comms = comms[:limit]
    log.info("PM ingest: %d communities", len(comms))
    sem = asyncio.Semaphore(concurrency or settings.pm_ingest_concurrency)
    stats = {"ok": 0, "no-location": 0, "no-hash": 0, "error": 0}

    async def _one(slug: str, district: str):
        async with sem:
            try:
                stats[await _ingest_one_community(slug, district)] += 1
            except Exception as e:
                stats["error"] += 1
                log.warning("community %s (%s) failed: %s", slug, district, e)

    await asyncio.gather(*[_one(s, d) for s, d in comms])
    log.info("PM ingest done: %s", stats)
    return stats


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = sys.argv[1:]
    lim = int(args[0]) if args and args[0].isdigit() else None
    asyncio.run(ingest_communities(limit=lim))
