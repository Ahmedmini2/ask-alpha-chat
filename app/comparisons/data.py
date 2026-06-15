"""Assemble the template context for a 2–3 way property comparison sheet.

Everything factual is computed from the database (units, market transactions)
or supplied as explicit per-project agent overrides. The one synthesised value
is the **Alpha Score** — a transparent 0–100 composite of the same investment
signals analyze_investment already returns (value vs market, momentum, activity,
yield band, payment plan). Metrics we can neither compute nor were given render
as "—"; the sheet never invents factual numbers (yields, appreciation).
"""
import logging
import statistics
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brochures import storage
from app.brochures.data import (
    _f,
    _size_to_sqft,
    clean_text,
    fmt_aed_compact,
    fmt_int,
    fmt_pct,
)
from app.db.models import Project, ProjectAsset
from app.tools.market import _analyze_one

log = logging.getLogger("askalpha.comparisons")

# Per-project override keys the agent may state in chat.
OVERRIDE_KEYS = (
    "net_yield_pct", "annual_appreciation_pct", "y5_projected_value_aed",
    "price_per_sqft_aed", "alpha_score",
)

_COUNT_WORDS = {2: "Two", 3: "Three"}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# per-project field computation
# --------------------------------------------------------------------------

def _bedrooms_cell(beds: list[float]) -> tuple[str, Optional[float]]:
    """Render the bedroom spread (e.g. 'Studio–4', '3') and the max for ranking."""
    vals = [b for b in beds if b is not None]
    if not vals:
        return ("—", None)
    lo, hi = min(vals), max(vals)

    def lab(v: float) -> str:
        if v == 0:
            return "Studio"
        return f"{int(v)}" if float(v).is_integer() else f"{v:g}"

    disp = lab(lo) if lo == hi else f"{lab(lo)}–{lab(hi)}"
    return (disp, hi)


def _unit_facts(project: Project) -> dict:
    """Bedrooms / bathrooms / typical size / entry price from a project's units."""
    beds, baths, sizes, prices = [], [], [], []
    for u in project.units:
        b = _f(u.bedrooms)
        if b is not None:
            beds.append(b)
        ba = _f(u.bathrooms)
        if ba is not None and ba > 0:
            baths.append(ba)
        unit_area = u.area_unit if (u.area_unit and u.area_unit.lower() != "none") else project.area_unit
        s = _size_to_sqft(u.size_from or u.size, unit_area)
        if s:
            sizes.append(s)
        for v in (u.price_from, u.price):
            fv = _f(v)
            if fv and fv > 0:
                prices.append(fv)

    bed_disp, bed_cmp = _bedrooms_cell(beds)
    bath_max = max(baths) if baths else None
    size_med = statistics.median(sizes) if sizes else None

    entry = min(prices) if prices else None
    mp = _f(project.min_price)
    if mp and mp > 0:
        entry = mp if entry is None else min(entry, mp)

    return {
        "bed_disp": bed_disp, "bed_cmp": bed_cmp,
        "bath_max": bath_max,
        "size_med": size_med,
        "entry": entry,
    }


def _price_per_sqft(analysis: dict, project: Project, ov: dict) -> Optional[float]:
    o = _f(ov.get("price_per_sqft_aed"))
    if o:
        return o
    a = _f(analysis.get("asking_rate_aed_sqft"))
    if a:
        return a
    mp, ms = _f(project.min_price), _size_to_sqft(project.min_size, project.area_unit)
    if mp and ms:
        return mp / ms
    return None


def _alpha_score(analysis: dict, ov: dict) -> Optional[int]:
    """Transparent 0–100 verdict from the investment signals we actually hold.

    Override wins. Otherwise score a neutral 55 baseline up/down by: value vs the
    area median (cheaper = better), 90-day momentum, activity label, the yield
    band midpoint, and a post-handover plan. Returns None only when NO signal is
    available (no units, no market) so the sheet can show '—' rather than a made-up
    figure.
    """
    o = _f(ov.get("alpha_score"))
    if o is not None:
        return int(round(_clamp(o, 0, 100)))

    score, signals = 68.0, 0

    prem = analysis.get("premium_to_market_pct")
    if prem is not None:
        # Cheaper than the area median lifts the score; a premium trims it (gently).
        score += _clamp(-_f(prem) * 0.45, -14, 12)
        signals += 1

    market = analysis.get("market") or {}
    mom = market.get("rate_momentum_pct")
    if mom is not None:
        score += _clamp(_f(mom) * 1.0, -6, 6)
        signals += 1
    act = (market.get("activity_label") or "").lower()
    act_adj = {"hot": 5, "healthy": 3, "active": 3, "cooling": -3, "quiet": -5, "slow": -5}
    if act in act_adj:
        score += act_adj[act]
        signals += 1

    ry = analysis.get("rental_yield_estimate") or {}
    lo, hi = _f(ry.get("gross_yield_low_pct")), _f(ry.get("gross_yield_high_pct"))
    if lo and hi:
        score += _clamp(((lo + hi) / 2 - 6) * 1.6, -7, 8)
        signals += 1

    if analysis.get("post_handover_plan"):
        score += 4

    if signals == 0:
        return None
    return int(round(_clamp(score, 40, 96)))


async def _first_image(db: AsyncSession, project: Project) -> Optional[bytes]:
    """Lowest-position stored image asset for the project thumbnail (no vision needed)."""
    assets = (await db.execute(
        select(ProjectAsset)
        .where(ProjectAsset.project_id == project.id, ProjectAsset.s3_key.is_not(None))
        .order_by(ProjectAsset.position)
    )).scalars().all()
    for a in assets:
        if str(a.status) == "stored" and str(a.kind) == "image" and (a.mime_type or "").startswith("image/"):
            b = await storage.fetch_asset_bytes(a.s3_bucket, a.s3_key)
            if b:
                return b
    return None


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------

def _winner(cmps: list[Optional[float]], direction: Optional[str]) -> Optional[int]:
    """Index of the unique best value; None if tied, all-empty, or no direction."""
    if not direction:
        return None
    present = [(i, v) for i, v in enumerate(cmps) if v is not None]
    if len(present) < 2:
        return None
    target = min(present, key=lambda t: t[1]) if direction == "min" else max(present, key=lambda t: t[1])
    ties = [t for t in present if abs(t[1] - target[1]) < 1e-9]
    return None if len(ties) > 1 else target[0]


# label, icon, direction, badge — the metric rows above the Alpha Score band.
ROW_SPECS = [
    ("price_sqft", "Price / sqft", "tag", "min", "Best"),
    ("ptype", "Property Type", "home", None, None),
    ("beds", "Bedrooms", "bed", "max", "Most"),
    ("baths", "Bathrooms", "bath", "max", "Most"),
    ("size", "Built-up Area", "area", "max", "Largest"),
    ("yield", "Rental Yield", "percent", "max", "Best"),
    ("appr", "Annual Appreciation", "trend", "max", "Best"),
    ("value5", "Value in 5 Years", "calendar", "max", "Highest"),
]


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

async def build_comparison_context(
    db: AsyncSession,
    projects: list[Project],
    overrides: Optional[list[dict]] = None,
) -> tuple[dict, dict[str, bytes]]:
    """Returns (template_context, image_files {relative_name: bytes})."""
    overrides = overrides or [{} for _ in projects]
    n = len(projects)
    files: dict[str, bytes] = {}

    # ---- per-project metric blocks ----
    metrics: list[dict] = []          # display + compare values, keyed by ROW_SPECS keys
    header_cards: list[dict] = []
    for i, project in enumerate(projects):
        ov = overrides[i] or {}
        analysis = await _analyze_one(db, project)
        facts = _unit_facts(project)

        # Shared Alpha Verdict (website parity): drives yield/appreciation/Y5 + the Alpha cell so
        # the comparison matches the chat + brochure + site. Agent overrides still win.
        from app.analytics.alpha_verdict import get_or_compute_verdict
        verdict = (await get_or_compute_verdict(db, project.id)) or {}
        vn = verdict.get("numbers", {}) or {}

        psf = _price_per_sqft(analysis, project, ov)
        net_yield = _f(ov.get("net_yield_pct"))
        if net_yield is None:
            net_yield = _f(vn.get("net_yield_pct"))
        if net_yield is None:  # last resort: the analyze_investment band midpoint
            ry = analysis.get("rental_yield_estimate") or {}
            lo, hi = _f(ry.get("gross_yield_low_pct")), _f(ry.get("gross_yield_high_pct"))
            if lo and hi:
                net_yield = round((lo + hi) / 2, 1)
        appr = _f(ov.get("annual_appreciation_pct"))
        if appr is None:
            appr = _f(vn.get("annual_appreciation_pct"))
        entry = facts["entry"]
        value5 = _f(ov.get("y5_projected_value_aed"))
        if value5 is None:
            value5 = _f(vn.get("y5_value_aed"))
        if value5 is None and appr is not None and entry:
            value5 = entry * (1 + appr / 100.0) ** 5
        # Alpha cell = the verdict conviction (BUY/WATCH/SKIP backed); override wins.
        alpha = _f(ov.get("alpha_score"))
        alpha = int(round(alpha)) if alpha is not None else (
            int(round(verdict["conviction"])) if verdict.get("conviction") is not None else None)

        dom_type = (analysis.get("dominant_unit_type") or "").strip()
        ptype = "—"
        if dom_type and dom_type != "default":
            ptype = dom_type.title()
            if ptype.endswith("s"):  # 'apartments' -> 'Apartment'
                ptype = ptype[:-1]

        metrics.append({
            "price_sqft": {"disp": ("AED " + fmt_int(psf)) if psf else "—", "cmp": psf},
            "ptype": {"disp": ptype, "cmp": None},
            "beds": {"disp": facts["bed_disp"], "cmp": facts["bed_cmp"]},
            "baths": {"disp": (f"{int(facts['bath_max'])}" if facts["bath_max"] else "—"),
                      "cmp": facts["bath_max"]},
            "size": {"disp": (f"{fmt_int(facts['size_med'])} sqft" if facts["size_med"] else "—"),
                     "cmp": facts["size_med"]},
            "yield": {"disp": fmt_pct(net_yield) or "—", "cmp": net_yield},
            "appr": {"disp": fmt_pct(appr, signed=True) if appr is not None else "—", "cmp": appr},
            "value5": {"disp": fmt_aed_compact(value5) or "—", "cmp": value5},
            "alpha": {"disp": (str(alpha) if alpha is not None else "—"), "cmp": alpha},
        })

        img_bytes = await _first_image(db, project)
        img_name = None
        if img_bytes:
            img_name = f"p{i}.jpg"
            files[img_name] = img_bytes
        district = clean_text(project.district)
        city = clean_text(project.city)
        location = " · ".join(x for x in (district, city) if x) or "United Arab Emirates"
        header_cards.append({
            "image": img_name,
            "name": project.name,
            "location": location,
            "price": fmt_aed_compact(entry) or "Price on request",
        })

    # ---- rank each row, attach badges ----
    # A row whose every cell is "—" (data we don't hold and weren't given — e.g.
    # bathrooms, or appreciation before the agent states it) is dropped rather than
    # printed as a dead all-blank line.
    rows = []
    for key, label, icon, direction, badge in ROW_SPECS:
        cells = [m[key]["disp"] for m in metrics]
        if all(c == "—" for c in cells):
            continue
        cmps = [m[key]["cmp"] for m in metrics]
        win = _winner(cmps, direction)
        rows.append({
            "label": label, "icon": icon, "badge": badge, "win": win,
            "cells": [{"disp": d, "is_win": (win == idx)} for idx, d in enumerate(cells)],
        })
    alpha_win = _winner([m["alpha"]["cmp"] for m in metrics], "max")
    alpha_row = {
        "label": "Alpha Score", "badge": "Best", "win": alpha_win,
        "cells": [{"disp": m["alpha"]["disp"], "is_win": (alpha_win == idx)} for idx, m in enumerate(metrics)],
    }

    cities = {clean_text(p.city) for p in projects if clean_text(p.city)}
    place = next(iter(cities)) if len(cities) == 1 else "UAE"
    count_word = _COUNT_WORDS.get(n, str(n))

    context = {
        "n": n,
        "label_w": "150px" if n >= 3 else "210px",
        "subtitle": (f"{count_word} {place} investments — price, yield, appreciation "
                     f"& Alpha verdict, at a glance."),
        "properties": header_cards,
        "rows": rows,
        "alpha": alpha_row,
    }
    return context, files
