"""Alpha Verdict — exact Python port of the aredxb-next website's lib/analyzers.ts +
lib/quickVerdict.ts, so ask-alpha's chat / PDFs / search show the SAME verdict + numbers as the
website for any project.

VERIFIED against the live site (project 2291, JVC, entry From AED 755K): with the static
COMMUNITY_DATA for JVC (yield 0.082, app 0.085, sqft 1100), entry unit ppsf 1728, 1-bed,
charges 18/sqft, 'yield' intent -> pillars 94 / 13 / 97 / 55, conviction 74.82 (WATCH, shown
"75"), net yield 7.6%, "57.1% above community average", Y5 ≈ AED 1.14M.

The DB layer (gather_verdict_inputs / recompute_verdict / get_or_compute_verdict) lives below the
pure core and is added once the project_alpha_verdict table exists.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("askalpha.alpha_verdict")


# Per-community model exactly as the site's COMMUNITY_DATA (yield & app are decimals; sqft = AED/sqft).
COMMUNITY_DATA: dict[str, dict] = {
    "dubai-marina":     {"label": "Dubai Marina",            "yield": 0.062, "app": 0.072, "sqft": 2300},
    "downtown":         {"label": "Downtown Dubai",          "yield": 0.058, "app": 0.068, "sqft": 2700},
    "palm-jumeirah":    {"label": "Palm Jumeirah",           "yield": 0.052, "app": 0.082, "sqft": 3800},
    "business-bay":     {"label": "Business Bay",            "yield": 0.068, "app": 0.075, "sqft": 1900},
    "jvc":              {"label": "Jumeirah Village Circle", "yield": 0.082, "app": 0.085, "sqft": 1100},
    "dubai-hills":      {"label": "Dubai Hills Estate",      "yield": 0.064, "app": 0.078, "sqft": 2100},
    "damac-lagoons":    {"label": "Damac Lagoons",           "yield": 0.075, "app": 0.092, "sqft": 1700},
    "emaar-beachfront": {"label": "Emaar Beachfront",        "yield": 0.061, "app": 0.095, "sqft": 3200},
    "sobha-hartland":   {"label": "Sobha Hartland",          "yield": 0.069, "app": 0.080, "sqft": 2000},
    "arabian-ranches":  {"label": "Arabian Ranches",         "yield": 0.058, "app": 0.062, "sqft": 1600},
}
_FALLBACK_SLUG = "dubai-marina"

DEFAULT_CHARGES_PER_SQFT = 18.0     # site default service charge used in net yield
DUBAI_YIELD_BENCHMARK = 0.07        # thesisScore ('yield' thesis) benchmark
FORMULA_VERSION = "v1"

# [yield, comp, thesis, risk] weights per intent (the site's quickVerdict uses 'yield').
INTENT_WEIGHTS: dict[str, tuple[float, float, float, float]] = {
    "yield":        (0.40, 0.20, 0.30, 0.10),
    "appreciation": (0.20, 0.30, 0.30, 0.20),
    "balanced":     (0.25, 0.25, 0.25, 0.25),
}

BASIS = (
    "Alpha Verdict is a 4-pillar composite (yield vs community, price/sqft vs community, yield vs "
    "Dubai benchmark, risk) on Allegiance's area model; numbers are area-model estimates, not live "
    "per-property data."
)


def canonical_community_slug(name: Optional[str]) -> str:
    """Mirror the website's canonicalCommunitySlug so we hit the same COMMUNITY_DATA entry."""
    s = " ".join((name or "").split()).lower()
    if "marina" in s:
        return "dubai-marina"
    if "downtown" in s or "burj khalifa" in s:
        return "downtown"
    if "palm jumeirah" in s or s == "palm":
        return "palm-jumeirah"
    if "business bay" in s:
        return "business-bay"
    if "jumeirah village circle" in s or "jvc" in s:
        return "jvc"
    if "dubai hills" in s:
        return "dubai-hills"
    if "lagoons" in s:
        return "damac-lagoons"
    if "beachfront" in s:
        return "emaar-beachfront"
    if "hartland" in s:
        return "sobha-hartland"
    if "arabian ranches" in s:
        return "arabian-ranches"
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


@dataclass(frozen=True)
class CommunityModel:
    slug: str
    label: str
    gross_yield: float
    appreciation: float
    ppsf: float
    matched: bool


def resolve_community(name: Optional[str]) -> CommunityModel:
    slug = canonical_community_slug(name)
    cd = COMMUNITY_DATA.get(slug)
    if cd:
        return CommunityModel(slug, cd["label"], cd["yield"], cd["app"], cd["sqft"], True)
    fb = COMMUNITY_DATA[_FALLBACK_SLUG]
    return CommunityModel(slug or _FALLBACK_SLUG, fb["label"], fb["yield"], fb["app"], fb["sqft"], False)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _bed_adj(beds: Optional[float]) -> float:
    b = 1.0 if beds is None else float(beds)
    if b <= 1:
        return 1.05
    if b >= 4:
        return 0.94
    return 1.0


def _est_size_sqft(beds: Optional[float]) -> float:
    b = 1.0 if beds is None else float(beds)
    return 850.0 + max(0.0, b - 1.0) * 400.0


def compute_alpha_verdict(
    *,
    price: Optional[float],
    community: "str | CommunityModel | None",
    beds: Optional[float] = None,
    sqft: Optional[float] = None,
    intent: str = "yield",
    charges_per_sqft: float = DEFAULT_CHARGES_PER_SQFT,
) -> Optional[dict]:
    """Exact port of computePropertyFit + verdictKind. Returns the verdict dict, or None if there's
    no price to score. `community` may be a name (resolved) or a CommunityModel."""
    if not price or price <= 0:
        return None
    cm = community if isinstance(community, CommunityModel) else resolve_community(community)
    if beds is None:
        beds = 1.0
    if not sqft or sqft <= 0:
        sqft = _est_size_sqft(beds)
    area = max(200.0, float(sqft))

    est_rent = price * cm.gross_yield * _bed_adj(beds)
    charges_annual = charges_per_sqft * area
    net_yield = (est_rent - charges_annual) / price
    ppsf = price / area
    sqft_ratio = ppsf / cm.ppsf if cm.ppsf else 1.0

    yield_score = _clamp((net_yield / cm.gross_yield) * 80 + 20) if cm.gross_yield else 0.0
    comp_score = _clamp(70 - (sqft_ratio - 1) * 100)
    thesis_score = _clamp((net_yield / DUBAI_YIELD_BENCHMARK) * 90)
    risk = 70.0
    if charges_per_sqft > 22:
        risk -= 20
    elif charges_per_sqft < 14:
        risk += 10
    if sqft_ratio > 1.15:
        risk -= 15
    elif sqft_ratio < 0.92:
        risk += 8
    risk_score = _clamp(risk)

    wy, wc, wt, wr = INTENT_WEIGHTS.get(intent, INTENT_WEIGHTS["balanced"])
    conviction = wy * yield_score + wc * comp_score + wt * thesis_score + wr * risk_score
    verdict = "BUY" if conviction >= 75 else ("WATCH" if conviction >= 55 else "SKIP")
    y5_value = price * (1 + cm.appreciation) ** 5

    return {
        "conviction": round(conviction, 2),          # unrounded threshold; display may round
        "verdict": verdict,
        "intent": intent,
        "pillars": {
            "yield": round(yield_score),
            "comp": round(comp_score),
            "thesis": round(thesis_score),
            "risk": round(risk_score),
        },
        "numbers": {
            "net_yield_pct": round(net_yield * 100, 1),
            "area_rent_return_pct": round(cm.gross_yield * 100, 1),
            "annual_appreciation_pct": round(cm.appreciation * 100, 1),
            "y5_value_aed": round(y5_value),
            "ppsf_aed": round(ppsf),
            "vs_area_price_pct": round((sqft_ratio - 1) * 100, 1),
        },
        "community_slug": cm.slug,
        "community_label": cm.label,
        "used_fallback": not cm.matched,
        "inputs": {
            "price_aed": float(price),
            "beds": float(beds),
            "size_sqft": round(area),
            "charges_per_sqft": charges_per_sqft,
            "intent": intent,
        },
        "formula_version": FORMULA_VERSION,
        "basis": BASIS,
    }


# ---------------------------------------------------------------------------------------------
# DB layer: PM-backed community stats (primary) with static fallback, + the verdict store.
# Imports are local to keep the pure core importable without the DB stack.
# ---------------------------------------------------------------------------------------------

def _f(x):
    return float(x) if x is not None else None


def _i(x):
    return int(round(float(x))) if x is not None else None


async def resolve_community_db(db, name: Optional[str]):
    """Build the community model used by the verdict, preferring REAL Property Monitor stats
    field-by-field and filling any gap from the static model. Returns
    (CommunityModel, source, service_charge_per_sqft|None, pm_stats_updated_at|None)."""
    from sqlalchemy import select
    from app.db.models import PmCommunityStats

    slug = canonical_community_slug(name)
    static = COMMUNITY_DATA.get(slug)
    fb = COMMUNITY_DATA[_FALLBACK_SLUG]
    base_yield = (static or fb)["yield"]
    base_app = (static or fb)["app"]
    base_ppsf = (static or fb)["sqft"]
    label = (static or {}).get("label") or slug

    pm = (await db.execute(
        select(PmCommunityStats).where(PmCommunityStats.community_slug == slug)
    )).scalar_one_or_none()
    if pm:
        # PM ppsf is genuinely per-community (real) — use it. ppsf/service-charge stay TRUTHY-guarded:
        # a 0.0 there is "PM has no figure", not a real value — a 0 ppsf would divide-by-zero the comp
        # pillar, and PM returns 0 service charge for communities that plainly have one (e.g. JVC), so
        # 0 must fall back to the 18/sqft default rather than inflate net yield. PM has no rental yield,
        # so yield stays from the model.
        #
        # appreciation, by contrast, uses is-not-None so a genuine FLAT market (0.0) is preserved. But
        # PM's appreciation (market-trends) is a DUBAI-WIDE index — the same value for every community
        # — so it would erase per-community differentiation and diverge from the website; we keep the
        # per-community model appreciation when this community is modeled, and only use PM's real
        # number for UNMODELED communities (better than the Dubai-Marina fallback).
        y = float(pm.gross_yield) if pm.gross_yield is not None else base_yield
        s = float(pm.ppsf_aed) if pm.ppsf_aed else base_ppsf
        sc = float(pm.service_charge_aed_sqft) if pm.service_charge_aed_sqft else None
        if static:
            a = base_app                                   # per-community model (matches website)
        else:
            a = float(pm.appreciation) if pm.appreciation is not None else base_app
        return (CommunityModel(slug, pm.community_label or label, y, a, s, True),
                "property_monitor", sc, pm.updated_at)

    cm = CommunityModel(slug, label, base_yield, base_app, base_ppsf, bool(static))
    return cm, ("static_model" if static else "static_fallback"), None, None


# Floor below which a unit's stored size is treated as bad data (else ppsf = price/200 explodes).
_MIN_PLAUSIBLE_SQFT = 150.0
_SQM_PER_SQFT = 10.7639


async def _entry_unit_sql(db, project) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Cheapest priced unit -> (price, beds, sqft_in_sqft). Converts sqm sizes to sqft and ignores
    implausible sizes (bad data). Falls back to the project's own min_price / min_size."""
    from sqlalchemy import text
    row = (await db.execute(text("""
        SELECT bedrooms,
               COALESCE(price_from, price) AS price,
               COALESCE(size_from, size)   AS size,
               COALESCE(NULLIF(lower(area_unit),'none'), :pu) AS area_unit
        FROM project_units
        WHERE project_id = :id AND COALESCE(price_from, price) > 0
        ORDER BY COALESCE(price_from, price) ASC
        LIMIT 1
    """), {"id": project.id, "pu": (project.area_unit or "sqft")})).mappings().first()
    if row and row["price"]:
        size = float(row["size"]) if row["size"] else None
        if size and str(row["area_unit"] or "").startswith("sqm"):
            size *= _SQM_PER_SQFT
        if size is not None and size < _MIN_PLAUSIBLE_SQFT:
            size = None
        beds = float(row["bedrooms"]) if row["bedrooms"] is not None else None
        return float(row["price"]), beds, size
    price = float(project.min_price) if project.min_price else None
    size = float(project.min_size) if project.min_size else None
    if size and (project.area_unit or "").lower().startswith("sqm"):
        size *= _SQM_PER_SQFT
    if size is not None and size < _MIN_PLAUSIBLE_SQFT:
        size = None
    return price, None, size


async def recompute_verdict(project_id: int) -> Optional[dict]:
    """Compute the verdict for a project from its entry unit + (PM or static) community stats and
    UPSERT it into project_alpha_verdict. Own session (safe to call from any consumer). Returns the
    verdict dict, or None if the project has no price to score."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.config import settings
    from app.db.session import AsyncSessionLocal
    from app.db.models import Project, ProjectAlphaVerdict

    async with AsyncSessionLocal() as db:
        project = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one_or_none()
        if project is None:
            return None
        price, beds, sqft = await _entry_unit_sql(db, project)
        cm, source, sc, stats_as_of = await resolve_community_db(db, project.district or project.city)
        charges = sc if sc is not None else DEFAULT_CHARGES_PER_SQFT
        v = compute_alpha_verdict(price=price, community=cm, beds=beds, sqft=sqft,
                                  intent=settings.alpha_verdict_intent, charges_per_sqft=charges)
        if v is None:
            return None
        now = datetime.now(timezone.utc)
        vals = {
            "project_id": project.id, "conviction": v["conviction"], "verdict": v["verdict"],
            "intent": v["intent"],
            "yield_score": v["pillars"]["yield"], "comp_score": v["pillars"]["comp"],
            "thesis_score": v["pillars"]["thesis"], "risk_score": v["pillars"]["risk"],
            "net_yield_pct": v["numbers"]["net_yield_pct"],
            "area_rent_return_pct": v["numbers"]["area_rent_return_pct"],
            "annual_appreciation_pct": v["numbers"]["annual_appreciation_pct"],
            "y5_value_aed": v["numbers"]["y5_value_aed"], "ppsf_aed": v["numbers"]["ppsf_aed"],
            "vs_area_price_pct": v["numbers"]["vs_area_price_pct"],
            "community_slug": v["community_slug"], "community_label": v["community_label"],
            "used_fallback": v["used_fallback"], "stats_source": source,
            "price_aed": price, "beds": beds, "size_sqft": v["inputs"]["size_sqft"],
            "inputs": v["inputs"], "basis": v["basis"], "formula_version": v["formula_version"],
            "computed_at": now,
            # When the verdict's stats came from PM, record when those stats were refreshed so the
            # freshness gate can recompute as soon as a newer PM ingest lands; else stamp now.
            "stats_as_of": stats_as_of or now,
        }
        stmt = pg_insert(ProjectAlphaVerdict).values(**vals)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ProjectAlphaVerdict.project_id],
            set_={k: stmt.excluded[k] for k in vals if k != "project_id"},
        )
        await db.execute(stmt)
        await db.commit()
        v["project_id"] = project.id
        v["stats_source"] = source
        return v


def _row_to_dict(row) -> dict:
    return {
        "project_id": row.project_id,
        "conviction": _f(row.conviction),
        "verdict": row.verdict,
        "intent": row.intent,
        "pillars": {"yield": _i(row.yield_score), "comp": _i(row.comp_score),
                    "thesis": _i(row.thesis_score), "risk": _i(row.risk_score)},
        "numbers": {
            "net_yield_pct": _f(row.net_yield_pct),
            "area_rent_return_pct": _f(row.area_rent_return_pct),
            "annual_appreciation_pct": _f(row.annual_appreciation_pct),
            "y5_value_aed": _f(row.y5_value_aed),
            "ppsf_aed": _f(row.ppsf_aed),
            "vs_area_price_pct": _f(row.vs_area_price_pct),
        },
        "community_slug": row.community_slug, "community_label": row.community_label,
        "used_fallback": row.used_fallback, "stats_source": row.stats_source,
        "basis": row.basis, "formula_version": row.formula_version,
    }


async def get_or_compute_verdict(db, project_id: int, max_age_days: Optional[int] = None) -> Optional[dict]:
    """The single accessor for every consumer. Reads the stored verdict via `db`; recomputes (own
    session) when missing, stale, or from an older formula version."""
    from sqlalchemy import select
    from app.config import settings
    from app.db.models import ProjectAlphaVerdict, PmCommunityStats

    if max_age_days is None:
        max_age_days = settings.alpha_verdict_max_age_days
    row = (await db.execute(
        select(ProjectAlphaVerdict).where(ProjectAlphaVerdict.project_id == project_id)
    )).scalar_one_or_none()
    fresh = (
        row is not None
        and row.formula_version == FORMULA_VERSION
        and row.computed_at is not None
        and (datetime.now(timezone.utc) - row.computed_at) < timedelta(days=max_age_days)
    )
    # Even within the age window, a verdict is stale if Property Monitor stats for its community were
    # refreshed AFTER it was computed (a PM ingest landed without a backfill). Recompute so chat picks
    # up the fresh numbers immediately instead of waiting out max_age_days.
    if fresh and row.community_slug:
        pm_updated = (await db.execute(
            select(PmCommunityStats.updated_at).where(
                PmCommunityStats.community_slug == row.community_slug)
        )).scalar_one_or_none()
        if pm_updated is not None and pm_updated > row.computed_at:
            fresh = False
    if fresh:
        return _row_to_dict(row)
    return await recompute_verdict(project_id)
