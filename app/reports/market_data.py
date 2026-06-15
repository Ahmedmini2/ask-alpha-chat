"""Data layer for the Dubai Market Report PDF.

Pulls a Dubai-wide market snapshot from REAL data — Property Monitor's price index
(pm_market_trends), per-community stats (pm_community_stats), and our Alpha Verdict store
(project_alpha_verdict) — and shapes it into the Jinja context the market_report template
renders. Pure formatters (money/pct/sparkline) are unit-tested; build_market_context never
raises (a missing source just drops that section).
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("askalpha.reports")


# --------------------------------------------------------------------------- pure formatters

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_money(n, *, aed: bool = True) -> str:
    """1658 -> 'AED 1,658'; 1_856_000 -> 'AED 1.86M'. None -> '—'."""
    v = _f(n)
    if v is None:
        return "—"
    pfx = "AED " if aed else ""
    if abs(v) >= 1_000_000:
        return f"{pfx}{v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{pfx}{v:,.0f}"
    return f"{pfx}{v:,.0f}"


def fmt_pct(x, *, signed: bool = False, decimals: int = 1) -> str:
    """0.0478 (fraction) or 4.78 (already pct)? This takes a PERCENT value already (4.78) and
    formats it. 4.78 -> '+4.78%' when signed. None -> '—'."""
    v = _f(x)
    if v is None:
        return "—"
    s = f"{v:+.{decimals}f}" if signed else f"{v:.{decimals}f}"
    return f"{s}%"


def sparkline_points(values: list, w: float, h: float, pad: float = 3.0) -> str:
    """Map a numeric series to an SVG polyline 'x,y x,y …' inside a w×h box (y inverted so
    higher values sit higher). Returns '' for an empty/degenerate series."""
    vals = [v for v in (_f(v) for v in (values or [])) if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    pts = []
    for i, v in enumerate(vals):
        x = pad + (inner_w * (i / (n - 1)) if n > 1 else inner_w / 2)
        y = pad + inner_h * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _slug_label(label: str, slug: str) -> str:
    """Prefer the curated community label; title-case anything that's really a slug.

    Unmodeled communities store the slug itself as the label (e.g. 'dubai-land-residence-complex'),
    so a hyphen-with-no-space label is treated as a slug and prettified. Curated labels with spaces
    or parentheses ('JVC (Jumeirah Village Circle)') pass through untouched."""
    s = (label or "").strip()
    if s and "-" in s and " " not in s:
        s = s.replace("-", " ").title()
    if not s:
        s = (slug or "").replace("-", " ").title()
    return s or "—"


# --------------------------------------------------------------------------- DB gather

async def _dubai_ppsf(db: AsyncSession) -> float | None:
    """Real Dubai-wide avg price/sqft = average of every community's indexed ppsf_aed.

    pm_community_stats holds one ppsf_aed per community, so this is an honest Dubai-residential
    aggregate rather than an arbitrary single community's price_sqft."""
    return _f((await db.execute(text(
        "SELECT avg(ppsf_aed) FROM pm_community_stats WHERE ppsf_aed IS NOT NULL"
    ))).scalar())


async def _market_index(db: AsyncSession) -> dict:
    """Dubai price-index headline + sparkline from pm_market_trends.

    pm_market_trends is keyed PER community (one row per community_slug — UNIQUE index), so there
    is no single "Dubai-wide" row. Pick a stable, representative series deterministically (prefer
    Dubai Marina, then the most recently fetched community, then slug) so the index / momentum /
    sparkline don't flip between arbitrary communities across runs or after a VACUUM. The headline
    price/sqft KPI is NOT taken from this single community's price_sqft — it is a real Dubai-wide
    aggregate computed by _dubai_ppsf()."""
    raw = (await db.execute(text(
        "SELECT raw FROM pm_market_trends "
        "ORDER BY (community_slug = 'dubai-marina') DESC, fetched_at DESC, community_slug LIMIT 1"
    ))).scalar_one_or_none()
    if not raw:
        return {}
    series = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else None)
    if not isinstance(series, list) or not series:
        return {}
    pts = [p for p in series if isinstance(p, dict)]
    if not pts:
        return {}
    last = pts[-1]
    first = pts[0]
    index_vals = [p.get("index_value") for p in pts]

    # PM's monthly series carries only index_value/price_sqft/yoy_change — there are no
    # mom_change/qoq_change keys — so derive momentum from the index_value series itself
    # (oldest→newest): MoM = last vs the previous month, QoQ = last vs three months back.
    def _pct_change(offset: int):
        if len(pts) <= offset:
            return None
        cur = _f(pts[-1].get("index_value"))
        prev = _f(pts[-1 - offset].get("index_value"))
        if cur is None or not prev:
            return None
        return (cur / prev - 1.0) * 100.0

    out = {
        "ppsf": _f(last.get("price_sqft")),
        "yoy": _f(last.get("yoy_change")),
        "mom": _pct_change(1),
        "qoq": _pct_change(3),
        "index_value": _f(last.get("index_value")),
        "index_base": _f(first.get("index_value")) or 100.0,
        "as_of": f"{last.get('mn', '')} {last.get('yr', '')}".strip(),
        "start_label": f"{first.get('yr', '')}",
        "end_label": f"{last.get('yr', '')}",
        "series": index_vals,
    }
    base = out["index_base"] or 100.0
    if out["index_value"]:
        out["growth_since_base"] = (out["index_value"] / base - 1.0) * 100.0
    return out


async def _top_communities(db: AsyncSession, limit: int = 8) -> list[dict]:
    """Top communities by average Alpha conviction (with real avg yield, ppsf, BUY count)."""
    rows = (await db.execute(text("""
        SELECT community_slug, max(community_label) AS label, count(*) AS n,
               avg(conviction) AS avg_conv, avg(net_yield_pct) AS avg_yield,
               avg(ppsf_aed) AS avg_ppsf, sum((verdict = 'BUY')::int) AS buys
        FROM project_alpha_verdict
        WHERE community_slug IS NOT NULL
        GROUP BY community_slug HAVING count(*) >= 5
        ORDER BY avg_conv DESC NULLS LAST LIMIT :lim
    """), {"lim": limit})).mappings().all()
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "rank": i,
            "name": _slug_label(r["label"], r["community_slug"]),
            "conviction": round(_f(r["avg_conv"]) or 0),
            "yield_disp": fmt_pct(r["avg_yield"], decimals=1),
            "ppsf_disp": fmt_money(r["avg_ppsf"]),
            "buys": int(r["buys"] or 0),
            "n": int(r["n"] or 0),
        })
    return out


async def _top_picks(db: AsyncSession, limit: int = 7) -> list[dict]:
    """Highest-conviction BUY-rated projects across Dubai."""
    rows = (await db.execute(text("""
        SELECT p.name, v.community_label, v.community_slug, v.conviction, v.verdict,
               v.net_yield_pct, v.ppsf_aed, p.min_price
        FROM project_alpha_verdict v JOIN projects p ON p.id = v.project_id
        WHERE p.is_published = true AND v.verdict = 'BUY'
        ORDER BY v.conviction DESC NULLS LAST LIMIT :lim
    """), {"lim": limit})).mappings().all()
    out = []
    for i, r in enumerate(rows, 1):
        price = _f(r["min_price"])
        out.append({
            "rank": i,
            "name": r["name"],
            "community": _slug_label(r["community_label"], r["community_slug"]),
            "conviction": round(_f(r["conviction"]) or 0),
            "verdict": r["verdict"],
            "yield_disp": fmt_pct(r["net_yield_pct"], decimals=1),
            "ppsf_disp": fmt_money(r["ppsf_aed"]),
            "price_disp": fmt_money(price) if price and price > 0 else "On request",
        })
    return out


async def _premium_communities(db: AsyncSession, limit: int = 6) -> list[dict]:
    """Most premium communities by Property Monitor price/sqft."""
    rows = (await db.execute(text("""
        SELECT community_label, community_slug, ppsf_aed, service_charge_aed_sqft
        FROM pm_community_stats WHERE ppsf_aed IS NOT NULL
        ORDER BY ppsf_aed DESC LIMIT :lim
    """), {"lim": limit})).mappings().all()
    return [{
        "name": _slug_label(r["community_label"], r["community_slug"]),
        "ppsf_disp": fmt_money(r["ppsf_aed"]),
    } for r in rows]


async def _verdict_distribution(db: AsyncSession) -> dict:
    rows = (await db.execute(text(
        "SELECT verdict, count(*) AS c FROM project_alpha_verdict GROUP BY verdict"
    ))).mappings().all()
    d = {r["verdict"]: int(r["c"]) for r in rows}
    buy, watch, skip = d.get("BUY", 0), d.get("WATCH", 0), d.get("SKIP", 0)
    total = buy + watch + skip
    return {
        "buy": buy, "watch": watch, "skip": skip, "total": total,
        "buy_pct": round(buy / total * 100) if total else 0,
        "watch_pct": round(watch / total * 100) if total else 0,
        "skip_pct": round(skip / total * 100) if total else 0,
    }


async def build_market_context(db: AsyncSession) -> tuple[dict, dict]:
    """Assemble the Jinja context for the Dubai Market Report. Returns (context, image_files);
    image_files is empty (the chart is inline SVG, logo/fonts come from static_dir)."""
    idx = await _market_index(db)
    dubai_ppsf = await _dubai_ppsf(db)
    communities = await _top_communities(db)
    picks = await _top_picks(db)
    premium = await _premium_communities(db)
    dist = await _verdict_distribution(db)
    comm_tracked = (await db.execute(text("SELECT count(*) FROM pm_community_stats"))).scalar() or 0

    now = datetime.now(timezone.utc)
    chart_w, chart_h = 714.0, 150.0
    series = idx.get("series") or []
    points = sparkline_points(series, chart_w, chart_h)
    area = ""
    if points:
        first_x = points.split(" ")[0].split(",")[0]
        last_x = points.split(" ")[-1].split(",")[0]
        area = f"{first_x},{chart_h:.1f} {points} {last_x},{chart_h:.1f}"

    kpis = [
        {"label": "Avg Price / sqft",
         "value": fmt_money(dubai_ppsf if dubai_ppsf is not None else idx.get("ppsf")),
         "sub": "Dubai residential"},
        {"label": "YoY Appreciation", "value": fmt_pct(idx.get("yoy"), signed=True, decimals=2),
         "sub": "trailing 12 months"},
        {"label": "Projects Scored", "value": f"{dist['total']:,}", "sub": "by Alpha Verdict"},
        {"label": "Communities", "value": f"{int(comm_tracked):,}", "sub": "tracked live"},
    ]

    context = {
        "generated_on": now.strftime("%-d %B %Y") if hasattr(now, "strftime") else "",
        "as_of": idx.get("as_of") or now.strftime("%B %Y"),
        "subtitle": ("A data-led snapshot of Dubai's residential investment market — pricing, "
                     "momentum and the highest-conviction communities and projects, ranked by the "
                     "Alpha Verdict."),
        "kpis": kpis,
        "index": idx,
        "chart": {
            "w": chart_w, "h": chart_h, "points": points, "area": area,
            "growth": fmt_pct(idx.get("growth_since_base"), signed=True, decimals=0)
            if idx.get("growth_since_base") is not None else None,
            "mom": fmt_pct(idx.get("mom"), signed=True, decimals=2),
            "qoq": fmt_pct(idx.get("qoq"), signed=True, decimals=2),
        },
        "communities": communities,
        "picks": picks,
        "premium": premium,
        "distribution": dist,
        "basis": ("Pricing, the price index and per-community price/sqft are sourced from Property "
                  "Monitor (Dubai). Conviction, verdicts and net yields are computed by the "
                  "Allegiance Alpha Verdict model from current listings. Figures are indicative, "
                  "not investment advice."),
    }
    return context, {}
