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
import re
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


def _extract_appreciation(trends: Any) -> Optional[float]:
    """Annual appreciation (decimal) from the sales market-trends series — the LAST monthly point's
    `yoy_change` (a percent like '4.78'). PM's market-trends is a monthly index list (mn/yr/
    index_value/price_sqft/yoy_change), oldest→newest. PM does NOT return a rental yield via these
    endpoints, so gross yield stays from our model."""
    d = _unwrap(trends)
    if isinstance(d, list) and d:
        last = d[-1] if isinstance(d[-1], dict) else {}
        yoy = _num(last.get("yoy_change"))
        if yoy is not None:
            return yoy / 100.0
    return None


def _extract_trend_ppsf(trends: Any) -> Optional[float]:
    """Latest transaction price/sqft from the sales market-trends series (fallback for ppsf)."""
    d = _unwrap(trends)
    if isinstance(d, list) and d and isinstance(d[-1], dict):
        return _num(d[-1].get("price_sqft"))
    return None


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


def _search_terms(district: str) -> list[str]:
    """Candidate PM queries from a project's district string, best first. The real community name is
    often inside a parenthetical ('JVC (Jumeirah Village Circle)' -> 'Jumeirah Village Circle'),
    which is what PM matches; then the de-parenthesized base; then the raw string."""
    terms: list[str] = []
    m = re.search(r"\((.*?)\)", district or "")
    if m and m.group(1).strip():
        terms.append(m.group(1).strip())
    base = re.sub(r"\(.*?\)", "", district or "").strip()
    if base:
        terms.append(base)
    if (district or "").strip():
        terms.append(district.strip())
    out: list[str] = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return out


def _pick_location(candidates: list[dict], community_name: str) -> Optional[dict]:
    """Pick an AVM-CAPABLE location (avm_status=1) in the community. Prefer a level-3 building (the
    community-level entry can 500 on AVM) and an apartment whose master_development matches; fall
    back to any AVM-capable result."""
    if not candidates:
        return None
    cl = community_name.lower()
    avm = [c for c in candidates if c.get("avm_status") in (1, "1")]
    if not avm:
        return None
    l3 = [c for c in avm if str(c.get("level")) == "3"]
    pool = l3 or avm
    apts = [c for c in pool if (c.get("location_unit_type") or "").lower() == "apartment"]
    pool = apts or pool
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
    loc = None
    for q in _search_terms(district):
        locs = _unwrap(await pm.search_locations(q, emirate_id=settings.pm_emirate_id)) or []
        loc = _pick_location(locs, q)
        if loc:
            break
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

    # Real PM stats: appreciation from the sales index (latest yoy_change); ppsf from the AVM's
    # indexed community average (fallback to the latest trend price/sqft). PM has no rental yield
    # via these endpoints, so gross_yield stays NULL and the verdict keeps the model yield.
    appreciation = _extract_appreciation(trends_raw) if trends_raw else None
    ppsf = avm["ppsf_aed"] or (_extract_trend_ppsf(trends_raw) if trends_raw else None)
    label = COMMUNITY_DATA.get(slug, {}).get("label") or district
    async with AsyncSessionLocal() as db:
        stmt = pg_insert(PmCommunityStats).values(
            community_slug=slug, community_label=label, gross_yield=None,
            appreciation=appreciation, ppsf_aed=ppsf,
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


# --------------------------------------------------------------------------- per-PROJECT AVM

async def _ingest_project_report(project_id: int, slug: str, location_id: int) -> str:
    """Run a project-specific AVM: preflight the project's ENTRY UNIT (size/beds) against its
    community's PM location, then consumer-avm, and upsert pm_reports(project_id)."""
    from app.analytics.alpha_verdict import _entry_unit_sql
    from app.db.models import Project
    from sqlalchemy import select as _select
    async with AsyncSessionLocal() as db:
        proj = (await db.execute(_select(Project).where(Project.id == project_id))).scalar_one_or_none()
        if proj is None:
            return "skip"
        _price, beds, sqft = await _entry_unit_sql(db, proj)
    sqft = sqft or settings.pm_default_size_sqft
    beds_s = str(int(beds)) if beds else settings.pm_default_bedrooms
    pre = _unwrap(await pm.create_avm(
        location_id=location_id, unit_size_sqft=sqft,
        property_type_id=settings.pm_default_property_type_id, bedrooms=beds_s))
    rh = (pre or {}).get("reportHash")
    if not rh:
        return "no-hash"
    avm = _extract_avm(await pm.get_avm_result(rh))
    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM pm_reports WHERE project_id = :pid"), {"pid": project_id})
        await db.execute(text(
            "INSERT INTO pm_reports (project_id, community_slug, pm_location_id, report_hash, report_id, "
            "bedrooms, unit_size_sqft, property_type_id, valuation_aed, valuation_low_aed, "
            "valuation_high_aed, ppsf_aed, service_charge_aed_sqft, annual_service_charge_aed, "
            "confidence_level, confidence_score, fetched_at) VALUES "
            "(:pid,:s,:lid,:h,:rid,:beds,:size,:ptid,:val,:low,:high,:ppsf,:sc,:asc,:cl,:cs, now())"),
            {"pid": project_id, "s": slug, "lid": location_id, "h": rh, "rid": avm["report_id"],
             "beds": beds_s, "size": sqft, "ptid": settings.pm_default_property_type_id,
             "val": avm["valuation_aed"], "low": avm["valuation_low_aed"], "high": avm["valuation_high_aed"],
             "ppsf": avm["ppsf_aed"], "sc": avm["service_charge_aed_sqft"],
             "asc": avm["annual_service_charge_aed"], "cl": avm["confidence_level"], "cs": avm["confidence_score"]})
        await db.commit()
    return "ok"


async def ingest_project_reports(limit: Optional[int] = None, concurrency: Optional[int] = None) -> dict:
    """Per-project AVM for every priceable published project whose community has a PM location."""
    if not pm.configured():
        return {"error": "not_configured"}
    async with AsyncSessionLocal() as db:
        locs = {r.community_slug: r.pm_location_id for r in (await db.execute(text(
            "SELECT community_slug, pm_location_id FROM pm_locations WHERE pm_location_id IS NOT NULL"
        ))).mappings()}
        projs = (await db.execute(text(
            "SELECT id, district, city FROM projects WHERE is_published AND min_price IS NOT NULL ORDER BY id"
        ))).all()
    targets = []
    for pid, district, city in projs:
        lid = locs.get(canonical_community_slug(district or city or ""))
        if lid:
            targets.append((pid, canonical_community_slug(district or city or ""), lid))
    if limit:
        targets = targets[:limit]
    log.info("PM per-project AVM: %d projects (in %d PM-covered communities)", len(targets), len(locs))
    sem = asyncio.Semaphore(concurrency or settings.pm_ingest_concurrency)
    stats = {"ok": 0, "no-hash": 0, "skip": 0, "error": 0}

    async def _one(pid, slug, lid):
        async with sem:
            try:
                stats[await _ingest_project_report(pid, slug, lid)] += 1
            except Exception as e:
                stats["error"] += 1
                log.warning("project %s AVM failed: %s", pid, e)

    for i in range(0, len(targets), 200):
        await asyncio.gather(*[_one(*t) for t in targets[i:i + 200]])
        log.info("per-project progress %d/%d %s", min(i + 200, len(targets)), len(targets), stats)
    log.info("PM per-project AVM done: %s", stats)
    return stats


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = sys.argv[1:]
    if args and args[0] == "projects":
        lim = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        asyncio.run(ingest_project_reports(limit=lim))
    else:
        lim = int(args[0]) if args and args[0].isdigit() else None
        asyncio.run(ingest_communities(limit=lim))
