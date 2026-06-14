"""Investment-metric heuristics — the area-model figures the public website shows.

These reproduce the website's deterministic estimator (the front-end's
``lib/analyzers.ts``): a small per-community lookup of assumed yield /
appreciation / average price-per-sqft plus a handful of constants. They are
area-MODEL ESTIMATES, NOT live per-property market data — every consumer must
label them as such.

Hybrid sourcing (decided with the product owner):
  * Where Alpha has REAL data we use it in place of the table value — the
    area rental-yield band (``investment_yield_assumptions``), the real district
    median price/sqft and area activity (``get_market_sentiment``).
  * The 10-community table is the FALLBACK when we have no transactions.
  * Unknown communities fall back to the Dubai Marina row, mirroring the website.

The pure ``compute_metrics`` does the arithmetic; the async ``gather_area_inputs``
pulls the real-data overrides from the DB so the brochure and the chat tool feed
the same numbers in.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal

log = logging.getLogger("askalpha.metrics")

SQM_PER_SQFT = 10.7639

# Per-community assumptions, decimals (0.062 == 6.2%). Ported verbatim from the
# website's COMMUNITY_DATA table; `sqft` is the assumed average AED/sqft.
COMMUNITY_DATA: dict[str, dict[str, Any]] = {
    "dubai-marina":     {"name": "Dubai Marina",       "yield": 0.062, "app": 0.072, "sqft": 2300},
    "downtown":         {"name": "Downtown Dubai",     "yield": 0.058, "app": 0.068, "sqft": 2700},
    "palm-jumeirah":    {"name": "Palm Jumeirah",      "yield": 0.052, "app": 0.082, "sqft": 3800},
    "business-bay":     {"name": "Business Bay",       "yield": 0.068, "app": 0.075, "sqft": 1900},
    "jvc":              {"name": "JVC",                "yield": 0.082, "app": 0.085, "sqft": 1100},
    "dubai-hills":      {"name": "Dubai Hills Estate", "yield": 0.064, "app": 0.078, "sqft": 2100},
    "damac-lagoons":    {"name": "Damac Lagoons",      "yield": 0.075, "app": 0.092, "sqft": 1700},
    "emaar-beachfront": {"name": "Emaar Beachfront",   "yield": 0.061, "app": 0.095, "sqft": 3200},
    "sobha-hartland":   {"name": "Sobha Hartland",     "yield": 0.069, "app": 0.080, "sqft": 2000},
    "arabian-ranches":  {"name": "Arabian Ranches",    "yield": 0.058, "app": 0.062, "sqft": 1600},
}
FALLBACK_SLUG = "dubai-marina"

# Constants (website parity).
SERVICE_CHARGE_PER_SQFT = 18.0
HOLD_YEARS = 5
MIN_AREA_SQFT = 200.0
DOM_SALE_DEFAULT = 90      # community-average days-on-market, sale
DOM_RENT_DEFAULT = 45      # community-average days-on-market, rent

# Real-data touch: the area activity label tunes the time-to-sell estimate.
_ACTIVITY_DOM_SALE = {"hot": 45, "healthy": 70, "cooling": 120, "quiet": 150}
_ACTIVITY_DOM_RENT = {"hot": 25, "healthy": 38, "cooling": 60, "quiet": 75}

# Map our DB district/city/community names onto the table's slugs. Our names
# ("Downtown Dubai", "Jumeirah Village Circle") don't slugify to the table keys
# ("downtown", "jvc"), so a naive port would always fall back — these aliases fix
# that. Each entry: table-slug -> substrings to look for in the (normalised) name.
_ALIASES: dict[str, tuple[str, ...]] = {
    "dubai-marina":     ("dubai marina", "marina"),
    "downtown":         ("downtown", "burj khalifa", "opera district"),
    "palm-jumeirah":    ("palm jumeirah", "palm jebel ali", "the palm", "palm"),
    "business-bay":     ("business bay",),
    "jvc":              ("jvc", "jumeirah village circle"),
    "dubai-hills":      ("dubai hills",),
    "damac-lagoons":    ("damac lagoons", "lagoons"),
    "emaar-beachfront": ("emaar beachfront", "beachfront"),
    "sobha-hartland":   ("sobha hartland", "hartland"),
    "arabian-ranches":  ("arabian ranches",),
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _norm(name: Optional[str]) -> str:
    """lowercase, drop non-breaking spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", (name or "").replace("\xa0", " ")).strip().lower()


def _slugify(name: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", _norm(name)))


def resolve_community(name: Optional[str]) -> tuple[str, bool]:
    """Return (table_slug, matched). `matched` is False when we fell back to the
    Dubai Marina baseline because the name isn't in the table."""
    norm = _norm(name)
    if not norm:
        return FALLBACK_SLUG, False
    slug = _slugify(norm)
    if slug in COMMUNITY_DATA:
        return slug, True
    for table_slug, needles in _ALIASES.items():
        if any(n in norm for n in needles):
            return table_slug, True
    return FALLBACK_SLUG, False


def _bed_factor(beds: Optional[float]) -> float:
    if beds is None:
        return 1.0
    if beds <= 1:
        return 1.05
    if beds >= 4:
        return 0.94
    return 1.0


def _time_to_sell(activity_label: Optional[str], is_rent: bool) -> tuple[int, bool]:
    """(days, used_real). Uses the area activity label when we have it, else the
    community-average constant."""
    table = _ACTIVITY_DOM_RENT if is_rent else _ACTIVITY_DOM_SALE
    default = DOM_RENT_DEFAULT if is_rent else DOM_SALE_DEFAULT
    if activity_label:
        d = table.get(activity_label.strip().lower())
        if d is not None:
            return d, True
    return default, False


# --------------------------------------------------------------------------
# pure compute
# --------------------------------------------------------------------------

def compute_metrics(
    price: Any,
    *,
    beds: Any = None,
    sqft: Any = None,
    community: Optional[str] = None,
    # Real-data overrides (hybrid). None -> use the community-table value.
    area_yield: Optional[float] = None,        # decimal gross yield, e.g. 0.062
    area_appreciation: Optional[float] = None,  # decimal annual appreciation
    area_ppsf: Optional[float] = None,          # real area median AED/sqft
    activity_label: Optional[str] = None,
    service_charge_per_sqft: float = SERVICE_CHARGE_PER_SQFT,
    is_rent: bool = False,
) -> Optional[dict]:
    """Compute the website's investment summary metrics for one property/unit.

    Returns percent-scaled numbers (5.4 == 5.4%) plus a per-metric ``sources``
    map. Returns None when there's no usable price.
    """
    price = _f(price)
    if not price or price <= 0:
        return None
    beds = _f(beds)
    sqft = _f(sqft)

    slug, matched = resolve_community(community)
    cd = COMMUNITY_DATA[slug]
    used_fallback = community is not None and not matched

    yld = area_yield if area_yield is not None else cd["yield"]
    app = area_appreciation if area_appreciation is not None else cd["app"]

    # Net yield. With unit-level beds+sqft we refine it (the website's listing
    # path); without them we surface the area yield directly (its off-plan path).
    if beds is not None and sqft and sqft > 0:
        area = max(MIN_AREA_SQFT, sqft)
        est_rent = price * yld * _bed_factor(beds)
        charges = service_charge_per_sqft * area
        net_yield = (est_rent - charges) / price
    else:
        net_yield = yld

    y5_value = price * (1 + app) ** HOLD_YEARS
    gain5 = (y5_value - price) / price

    tts_days, tts_real = _time_to_sell(activity_label, is_rent)

    price_per_sqft = price / sqft if (sqft and sqft > 0) else None
    ref_sqft = area_ppsf if area_ppsf is not None else cd["sqft"]
    vs_area = ((price_per_sqft - ref_sqft) / ref_sqft * 100.0) if price_per_sqft and ref_sqft else None

    def _area_src(real: bool) -> str:
        if real:
            return "real"
        return "area_model_fallback" if used_fallback else "area_model"

    return {
        "community_matched": cd["name"],
        "community_slug": slug,
        "used_fallback": used_fallback,
        "net_yield_pct": round(net_yield * 100, 1),
        "area_avg_rent_return_pct": round(yld * 100, 1),
        "annual_appreciation_pct": round(app * 100, 1),
        "y5_projected_value_aed": round(y5_value, 0),
        "five_year_gain_pct": round(gain5 * 100, 1),
        "time_to_sell_days": tts_days,
        "price_per_sqft_aed": round(price_per_sqft, 0) if price_per_sqft else None,
        "vs_area_price_pct": round(vs_area, 1) if vs_area is not None else None,
        "sources": {
            "net_yield": _area_src(area_yield is not None),
            "area_avg_rent_return": _area_src(area_yield is not None),
            "annual_appreciation": _area_src(area_appreciation is not None),
            "y5_projected_value": _area_src(area_appreciation is not None),
            "time_to_sell": "real" if tts_real else "area_model",
            "vs_area_price": "real" if area_ppsf is not None else _area_src(False),
        },
    }


BASIS = (
    "Area-model ESTIMATE — not live per-property data. Net yield and area rent "
    "return use Allegiance's real area rental-yield band where available; "
    "appreciation/5-year value come from the area growth model; time-to-sell is "
    "tuned by real area activity. Unknown communities use a Dubai baseline."
)


# --------------------------------------------------------------------------
# real-data gathering (the hybrid layer)
# --------------------------------------------------------------------------

async def _area_yield_band(db: AsyncSession, project_id: int) -> Optional[float]:
    """Midpoint of the real gross-yield band for the project's dominant unit type,
    as a decimal. Falls back to the 'default' band, then None."""
    # NB: read with .scalar() — never alias the column 't', which collides with the
    # deprecated Row.t tuple accessor and silently returns the whole Row.
    dom = (await db.execute(text("""
        SELECT mode() WITHIN GROUP (ORDER BY lower(unit_type))
        FROM project_units WHERE project_id = :id AND unit_type IS NOT NULL
    """), {"id": project_id})).scalar()
    dom = (dom if isinstance(dom, str) else None) or "default"
    yr = (await db.execute(text("""
        SELECT gross_yield_low, gross_yield_high FROM investment_yield_assumptions
        WHERE property_type = :t
    """), {"t": dom})).mappings().first()
    if not yr:
        yr = (await db.execute(text("""
            SELECT gross_yield_low, gross_yield_high FROM investment_yield_assumptions
            WHERE property_type = 'default'
        """))).mappings().first()
    if not yr:
        return None
    lo, hi = _f(yr["gross_yield_low"]), _f(yr["gross_yield_high"])
    if lo is None or hi is None:
        return None
    return (lo + hi) / 2.0 / 100.0


async def _sentiment(db: AsyncSession, area: str) -> Optional[dict]:
    import json
    area = re.sub(r"\s+", " ", (area or "").replace("\xa0", " ")).strip()
    if not area:
        return None
    row = (await db.execute(text("SELECT get_market_sentiment(:q) AS s"), {"q": area})).mappings().first()
    s = row["s"] if row else None
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            s = None
    return s if (isinstance(s, dict) and s.get("matched_name") is not None) else None


async def _apply_sentiment(db: AsyncSession, area: str, out: dict) -> None:
    s = await _sentiment(db, area)
    if s:
        rate_sqm = _f(s.get("median_rate_sqm_12m"))
        if rate_sqm:
            out["area_ppsf"] = round(rate_sqm / SQM_PER_SQFT, 0)
        out["activity_label"] = s.get("activity_label")
        out["momentum_pct"] = s.get("rate_momentum_pct")


async def gather_area_inputs(project) -> dict:
    """Pull the real-data overrides for a project. Always returns a dict with the
    same keys (values None when unavailable) so it can be splatted straight into
    ``compute_metrics``. Never raises.

    Runs on its OWN session so an enrichment failure (e.g. a bad query aborting the
    transaction) can never poison the caller's transaction or expire its ORM
    objects — a DB hiccup just means we fall back to the area-table values."""
    out = {"area_yield": None, "area_appreciation": None, "area_ppsf": None,
           "activity_label": None, "momentum_pct": None}
    try:
        async with AsyncSessionLocal() as s:
            out["area_yield"] = await _area_yield_band(s, project.id)
            await _apply_sentiment(s, project.district or project.city or "", out)
    except Exception as e:
        log.warning("gather_area_inputs failed for project %s: %s", getattr(project, "id", "?"), e)
    return out


async def gather_area_inputs_by_area(area: str) -> dict:
    """Like gather_area_inputs but for a bare area name (no project). Uses the
    'default' yield band since we have no unit mix to pick a dominant type. Runs on
    its own session for the same isolation reasons."""
    out = {"area_yield": None, "area_appreciation": None, "area_ppsf": None,
           "activity_label": None, "momentum_pct": None}
    try:
        async with AsyncSessionLocal() as s:
            yr = (await s.execute(text("""
                SELECT gross_yield_low, gross_yield_high FROM investment_yield_assumptions
                WHERE property_type = 'default'
            """))).mappings().first()
            if yr:
                lo, hi = _f(yr["gross_yield_low"]), _f(yr["gross_yield_high"])
                if lo is not None and hi is not None:
                    out["area_yield"] = (lo + hi) / 2.0 / 100.0
            await _apply_sentiment(s, area or "", out)
    except Exception as e:
        log.warning("gather_area_inputs_by_area failed for %r: %s", area, e)
    return out
