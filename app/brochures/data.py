"""Assemble the template context for a project's mini brochure.

Everything factual comes from the database (or explicit agent-supplied
overrides). Metrics we cannot compute or weren't given render as "—" — the
brochure never invents numbers.
"""
import logging
import math
import re
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import property_metrics as metrics
from app.brochures import captions, storage
from app.db.models import Developer, Project, ProjectAsset, ProjectUnit

log = logging.getLogger("askalpha.brochures")

SQM_TO_SQFT = 10.7639
GOLDEN_VISA_AED = 2_000_000

# Hand-picked Dubai reference destinations for the drive-time grid and map dots.
LANDMARKS = [
    ("Downtown Dubai", 25.1972, 55.2744),
    ("Dubai Int'l Airport (DXB)", 25.2532, 55.3657),
    ("Mall of the Emirates", 25.1181, 55.2008),
    ("Palm Jumeirah", 25.1124, 55.1390),
    ("Dubai Marina", 25.0805, 55.1403),
    ("Expo City Dubai", 24.9645, 55.1450),
    ("Al Maktoum Airport (DWC)", 24.8965, 55.1614),
    ("Global Village", 25.0703, 55.3046),
    ("Burj Al Arab", 25.1412, 55.1853),
    ("Dubai Hills Mall", 25.1029, 55.2448),
]

_OUTDOOR_AMENITY_HINTS = (
    "pool", "garden", "park", "beach", "bbq", "barbecue", "jog", "walk", "track",
    "court", "playground", "play area", "outdoor", "terrace", "lagoon", "marina",
    "promenade", "plaza", "amphitheatre", "yoga", "sports", "cycling", "pet",
)

_FEATURE_ICON_RULES = [
    ("pool", ("pool", "lagoon", "swim")),
    ("gym", ("gym", "fitness")),
    ("spa", ("spa", "sauna", "wellness", "steam")),
    ("kids", ("kid", "child", "nursery")),
    ("cinema", ("cinema", "theater", "theatre", "screening")),
    ("garden", ("garden", "park", "green", "landscap")),
    ("sport", ("court", "sport", "padel", "tennis", "basket")),
    ("security", ("security", "gated", "concierge")),
    ("beach", ("beach", "shore", "sea ")),
    ("retail", ("retail", "shop", "mall", "boutique")),
    ("lounge", ("lounge", "club", "co-working", "cowork", "library")),
    ("bbq", ("bbq", "barbecue")),
    ("yoga", ("yoga", "meditat")),
]


# --------------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def fmt_aed_compact(amount: Any) -> Optional[str]:
    """2684000 -> 'AED 2.68M', 1000000 -> 'AED 1M', 737000 -> 'AED 737K'."""
    v = _f(amount)
    if not v or v <= 0:
        return None
    if v >= 1_000_000:
        m = v / 1_000_000
        s = f"{m:.2f}".rstrip("0").rstrip(".")
        return f"AED {s}M"
    k = v / 1_000
    s = f"{k:.0f}"
    return f"AED {s}K"


def fmt_int(v: Any) -> Optional[str]:
    x = _f(v)
    return f"{x:,.0f}" if x and x > 0 else None


def fmt_pct(v: Any, signed: bool = False) -> Optional[str]:
    x = _f(v)
    if x is None:
        return None
    s = f"{abs(x):.1f}".rstrip("0").rstrip(".")
    if signed:
        return f"+{s}%" if x >= 0 else f"−{s}%"
    return f"{s}%"


def fmt_quarter(q: Optional[str]) -> Optional[str]:
    """'2029-Q2' -> "Q2 '29"."""
    if not q:
        return None
    m = re.match(r"(\d{4})-Q(\d)", q.strip())
    if not m:
        return q
    return f"Q{m.group(2)} '{m.group(1)[2:]}"


def clean_text(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _strip_markdown(s: str) -> str:
    s = re.sub(r"#{1,6}[^\n]*", " ", s)            # headings (incl. '##### Project general facts')
    s = re.sub(r"[*_`>#\[\]()]+", " ", s)
    return clean_text(s)


def _first_sentences(s: str, max_chars: int) -> str:
    s = _strip_markdown(s)
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    for sep in (". ", "; "):
        idx = cut.rfind(sep)
        if idx > max_chars // 2:
            return cut[: idx + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + "…"


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _drive_minutes(km: float) -> int:
    # Straight-line distance x 1.35 route factor at ~50 km/h, plus local streets.
    return max(4, round(km * 1.35 / 50 * 60 + 6))


def _bedrooms_label(b: Any) -> tuple[str, str]:
    """1 -> ('One Bedroom', '1 BR'); 0 -> ('Studio', 'Studio'); 4.5 -> ('4.5 Bedroom', '4.5 BR')."""
    v = _f(b)
    if v is None:
        return ("Residence", "")
    if v == 0:
        return ("Studio", "Studio")
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven"}
    if v == int(v) and int(v) in words:
        return (f"{words[int(v)]} Bedroom", f"{int(v)} BR")
    n = f"{v:g}"
    return (f"{n} Bedroom", f"{n} BR")


def _size_to_sqft(size: Any, unit: Optional[str]) -> Optional[float]:
    v = _f(size)
    if not v or v <= 0:
        return None
    if (unit or "").lower().startswith("sqm"):
        return v * SQM_TO_SQFT
    return v


# --------------------------------------------------------------------------
# project resolution (same spirit as market.py: ilike, then trigram)
# --------------------------------------------------------------------------

async def resolve_project(db: AsyncSession, project_id: Optional[int], project_name: Optional[str]) -> Optional[Project]:
    if project_id:
        return (await db.execute(select(Project).where(Project.id == int(project_id)))).scalar_one_or_none()
    name = clean_text(project_name)
    if not name:
        return None
    p = (await db.execute(
        select(Project).where(Project.is_published, Project.name.ilike(f"%{name}%")).limit(1)
    )).scalar_one_or_none()
    if p is not None:
        return p
    row = (await db.execute(text(
        "select id from projects where is_published and similarity(name, :n) > 0.45 "
        "order by similarity(name, :n) desc limit 1"
    ), {"n": name})).first()
    if row is None:
        return None
    return (await db.execute(select(Project).where(Project.id == row.id))).scalar_one_or_none()


# --------------------------------------------------------------------------
# data gathering
# --------------------------------------------------------------------------

async def _district_median_price_sqft(db: AsyncSession, project: Project) -> Optional[float]:
    district = clean_text(project.district)
    if not district:
        return None
    row = (await db.execute(text("""
        select percentile_cont(0.5) within group (order by ppsf) med, count(*) n
        from (
            select (min_price / (case when lower(coalesce(area_unit,'sqft')) like 'sqm%'
                                      then min_size * 10.7639 else min_size end)) ppsf
            from projects
            where is_published and id <> :pid
              and regexp_replace(replace(coalesce(district,''), chr(160), ' '), '\\s+', ' ', 'g') ilike :d
              and coalesce(min_price,0) > 0 and coalesce(min_size,0) > 0
        ) t
        where ppsf between 200 and 20000
    """), {"pid": project.id, "d": district})).first()
    if row is None or row.n is None or row.n < 3:
        return None
    return _f(row.med)


async def _gather_units(db: AsyncSession, project: Project) -> list[dict]:
    """Group available units by bedroom count -> pricing rows + plan candidates."""
    units = (await db.execute(
        select(ProjectUnit).where(ProjectUnit.project_id == project.id)
    )).scalars().all()
    groups: dict[float, dict] = {}
    for u in units:
        b = _f(u.bedrooms)
        if b is None:
            continue
        price = _f(u.price_from) or _f(u.price)
        unit_area = u.area_unit if (u.area_unit and u.area_unit.lower() != "none") else project.area_unit
        size_lo = _size_to_sqft(u.size_from or u.size, unit_area)
        size_hi = _size_to_sqft(u.size_to, unit_area)
        g = groups.setdefault(b, {
            "bedrooms": b, "n": 0, "price_from": None, "size_from": None, "size_to": None,
            "views": set(), "unit_type": None, "layout_names": [], "plan_urls": [],
        })
        g["n"] += 1
        if price and price > 0 and (g["price_from"] is None or price < g["price_from"]):
            g["price_from"] = price
        if size_lo and (g["size_from"] is None or size_lo < g["size_from"]):
            g["size_from"] = size_lo
        for s in (size_lo, size_hi):
            if s and (g["size_to"] is None or s > g["size_to"]):
                g["size_to"] = s
        if u.view:
            g["views"].add(clean_text(u.view))
        if u.unit_type:
            g["unit_type"] = u.unit_type
        if u.layout_name:
            g["layout_names"].append(clean_text(u.layout_name))
        if u.plan_image_url:
            g["plan_urls"].append(u.plan_image_url)
        for li in (u.layout_images or []):
            url = li.get("url") if isinstance(li, dict) else None
            if url:
                g["plan_urls"].append(url)
    return [groups[k] for k in sorted(groups)]


async def _gather_assets(db: AsyncSession, project: Project) -> tuple[list[ProjectAsset], list[ProjectAsset]]:
    # status/kind are Postgres enums mapped as Text in the ORM — comparing them in
    # SQL fails (enum vs varchar), so filter in Python; per-project assets are few.
    assets = (await db.execute(
        select(ProjectAsset)
        .where(ProjectAsset.project_id == project.id,
               ProjectAsset.s3_key.is_not(None))
        .order_by(ProjectAsset.position)
    )).scalars().all()
    assets = [a for a in assets if str(a.status) == "stored"]
    images = [a for a in assets if str(a.kind) == "image" and (a.mime_type or "").startswith("image/")]
    plans = [a for a in assets if str(a.kind) == "floor_plan"]
    return images, plans


def _amenity_names(project: Project) -> list[str]:
    raw = project.raw or {}
    names: list[str] = []
    for item in (raw.get("project_amenities") or []):
        if not isinstance(item, dict):
            continue
        am = item.get("amenity") or {}
        name = am.get("name") or item.get("name")
        if name:
            name = clean_text(str(name))
            if name and name.lower() not in (n.lower() for n in names):
                names.append(name)
    for item in (project.amenities or []):
        if isinstance(item, dict):
            name = item.get("name") or item.get("title")
        else:
            name = item
        if name:
            name = clean_text(str(name))
            if name and name.lower() not in (n.lower() for n in names):
                names.append(name)
    return names


def _split_amenities(names: list[str]) -> tuple[list[str], list[str]]:
    outdoor, indoor = [], []
    for n in names:
        low = n.lower()
        (outdoor if any(h in low for h in _OUTDOOR_AMENITY_HINTS) else indoor).append(n)
    return outdoor[:8], indoor[:8]


def _payment_plan(project: Project, handover: Optional[str]) -> Optional[dict]:
    plans = (project.raw or {}).get("payment_plans") or []
    if not plans or not isinstance(plans[0], dict):
        return None
    steps_raw = plans[0].get("steps") or []
    flat: list[dict] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        children = [c for c in (s.get("children") or []) if isinstance(c, dict) and _f(c.get("percentage"))]
        flat.extend(children if children else [s])
    steps = []
    for s in flat:
        pct = _f(s.get("percentage"))
        if pct is None or pct <= 0:
            continue
        steps.append({"pct": pct, "name": clean_text(s.get("name") or ""), "stage": s.get("stage_type") or ""})
    if len(steps) < 2:
        return None
    if len(steps) > 7:
        # Grid is happiest at <= 7 cells. Merge the MIDDLE (construction) installments
        # into one cell but keep the final handover step intact — folding it in would
        # mislabel the handover payment and corrupt the X/Y split in the summary.
        final_step = steps[-1]
        head, mid = steps[:5], steps[5:-1]
        merged = {"pct": sum(t["pct"] for t in mid), "name": mid[-1]["name"], "stage": mid[-1]["stage"]}
        steps = head + [merged, final_step]

    ordinals = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th"]
    cells = []
    for i, s in enumerate(steps):
        is_final = i == len(steps) - 1
        pct_s = f"{s['pct']:g}%"
        if is_final and s["stage"] in ("on_handover", "post_handover", ""):
            label, when = "On Completion", (handover or s["name"] or None)
        else:
            label = f"{ordinals[min(i, 6)]} Installment"
            when = s["name"] or None
        cells.append({"pct": pct_s, "label": label, "when": when})

    pre = sum(s["pct"] for s in steps[:-1])
    final = steps[-1]["pct"]
    summary_bits = [f"{pre:g} / {final:g} · spread across construction, balance on completion"]
    if handover:
        summary_bits.append(f"anticipated handover {handover}")
    return {"steps": cells, "summary": " · ".join(summary_bits)}


# --------------------------------------------------------------------------
# photo planning
# --------------------------------------------------------------------------

_GALLERY_PREFS = ["living_room", "kitchen", "bedroom", "bathroom", "balcony_terrace", "building_exterior", "pool", "garden"]
_OUT_AMEN_PREFS = ["pool", "garden", "building_exterior", "kids_area"]
_IN_AMEN_PREFS = ["lobby", "gym", "spa", "kids_area", "amenity_other", "living_room", "kitchen"]

_DEFAULT_CAPTIONS = {
    "building_exterior": "Exterior", "living_room": "Living Room", "kitchen": "Kitchen",
    "bedroom": "Bedroom", "bathroom": "Bathroom", "balcony_terrace": "Terrace",
    "pool": "Pool", "garden": "Gardens", "gym": "Gym", "lobby": "Lobby",
    "kids_area": "Kids' Club", "spa": "Spa", "amenity_other": "Amenities", "other": "",
}


class _PhotoPool:
    def __init__(self, photos: list[dict]):
        self.photos = photos
        self.used: set[int] = set()

    def take(self, prefs: list[str], strict: bool = False) -> Optional[dict]:
        """Pick the first unused photo matching `prefs` in preference order.

        strict=True returns None when no category matches — used for the amenity
        columns so a lobby shot never lands under "Outdoor Amenities".
        """
        for cat in prefs:
            for i, p in enumerate(self.photos):
                if i not in self.used and p["category"] == cat:
                    self.used.add(i)
                    return p
        if strict:
            return None
        for i, p in enumerate(self.photos):  # any unused, skipping plans/diagrams
            if i not in self.used and p["category"] not in ("floor_plan", "map_or_diagram"):
                self.used.add(i)
                return p
        return None


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

async def build_context(
    db: AsyncSession,
    project: Project,
    agent: dict,
    overrides: Optional[dict] = None,
) -> tuple[dict, dict[str, bytes]]:
    """Returns (template_context, image_files {relative_name: bytes})."""
    ov = overrides or {}
    files: dict[str, bytes] = {}

    developer = None
    if project.developer_id:
        developer = (await db.execute(
            select(Developer).where(Developer.id == project.developer_id)
        )).scalar_one_or_none()

    district = clean_text(project.district)
    city = clean_text(project.city) or "Dubai"
    handover = fmt_quarter(project.completion_quarter)

    # ---- photos: fetch + classify in batches, assign to layout slots ----
    images, plan_assets = await _gather_assets(db, project)
    photos: list[dict] = []
    fetched_through = 0

    async def _classify_batch(n: int) -> None:
        nonlocal fetched_through
        batch = images[fetched_through:fetched_through + n]
        fetched_through += len(batch)
        blobs, kept = [], []
        for a in batch:
            b = await storage.fetch_asset_bytes(a.s3_bucket, a.s3_key)
            if b:
                blobs.append(b)
                kept.append(a)
        classified = await captions.classify_photos(blobs)
        for b, c in zip(blobs, classified):
            fname = f"img_{len(photos)}.jpg"
            files[fname] = b
            photos.append({
                "src": fname,
                "category": c["category"],
                "caption": c["caption"] or _DEFAULT_CAPTIONS.get(c["category"], ""),
            })

    await _classify_batch(18)
    pool = _PhotoPool(photos)

    cover_photo = pool.take(["building_exterior", "pool", "garden", "balcony_terrace"])
    gallery_photos = []
    for _ in range(5):
        p = pool.take(_GALLERY_PREFS)
        if p:
            gallery_photos.append({"src": p["src"], "caption": p["caption"] or "Interiors"})
    out_hero = pool.take(_OUT_AMEN_PREFS, strict=True)
    # Interior-heavy feeds often bury the pool/garden renders — keep looking in
    # batches (bounded) until we find outdoor material or run out.
    while out_hero is None and fetched_through < min(len(images), 48):
        await _classify_batch(12)
        out_hero = pool.take(_OUT_AMEN_PREFS, strict=True)
    out_duo = [p for p in (pool.take(_OUT_AMEN_PREFS, strict=True), pool.take(_OUT_AMEN_PREFS, strict=True)) if p]
    in_hero = pool.take(_IN_AMEN_PREFS, strict=True)
    in_duo = [p for p in (pool.take(_IN_AMEN_PREFS, strict=True), pool.take(_IN_AMEN_PREFS, strict=True)) if p]

    # ---- units / pricing rows / floor plans ----
    unit_groups = await _gather_units(db, project)
    area_unit_label = "sqft"

    pricing_rows = []
    for g in unit_groups[:4]:
        name, br = _bedrooms_label(g["bedrooms"])
        views = sorted(v for v in g["views"] if v)
        config = " · ".join(views[:2]) if views else (district or city)
        price = fmt_aed_compact(g["price_from"]) or "On request"
        size = fmt_int(g["size_from"]) or "—"
        sub = br if br else None
        if g.get("unit_type"):
            ut = clean_text(str(g["unit_type"])).rstrip("s").title()
            sub = f"{br} · {ut}" if br else ut
        pricing_rows.append({
            "name": name, "sub": sub, "size": size, "size_unit": area_unit_label,
            "config": config, "price": price,
        })

    # floor-plan images: only our own stored floor_plan assets (never Reelly URLs).
    # Match a group's Reelly layout URLs against asset.source_url to find our copy.
    used_plan_ids: set[int] = set()

    def _match_plan_asset(g: dict) -> Optional[ProjectAsset]:
        tails = set()
        for url in g["plan_urls"]:
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                tails.add(tail.split("?")[0])
        for a in plan_assets:
            if a.id in used_plan_ids:
                continue
            src_tail = (a.source_url or "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if src_tail and any(t.startswith(src_tail[:24]) or src_tail.startswith(t[:24]) for t in tails):
                used_plan_ids.add(a.id)
                return a
        return None

    plan_cards = []
    plan_groups = [g for g in unit_groups if g["plan_urls"]][:3] or unit_groups[:3]
    for i, g in enumerate(plan_groups):
        name, br = _bedrooms_label(g["bedrooms"])
        asset = _match_plan_asset(g)
        img_name = None
        if asset is None and plan_assets:
            for a in plan_assets:  # positional fallback
                if a.id not in used_plan_ids:
                    used_plan_ids.add(a.id)
                    asset = a
                    break
        if asset is not None:
            b = await storage.fetch_asset_bytes(asset.s3_bucket, asset.s3_key)
            if b:
                img_name = f"plan_{i}.jpg"
                files[img_name] = b
        parts = name.split(" ", 1)
        areas = []
        lo, hi = fmt_int(g["size_from"]), fmt_int(g["size_to"])
        if lo and hi and lo != hi:
            areas = [{"k": "From", "v": lo}, {"k": "Up to", "v": f"{hi} {area_unit_label}"}]
        elif lo:
            areas = [{"k": "Size", "v": f"{lo} {area_unit_label}"}]
        layouts = sorted(set(g["layout_names"]))
        type_label = layouts[0] if len(layouts) == 1 else (f"{len(layouts)} layouts" if layouts else br)
        plan_cards.append({
            "name": name, "name_accent": parts[0], "name_rest": parts[1] if len(parts) > 1 else "",
            "type_label": type_label, "image": img_name, "areas": areas,
        })

    # ---- amenities ----
    amenity_names = _amenity_names(project)
    outdoor, indoor = _split_amenities(amenity_names)
    am_columns = []
    if outdoor or out_hero:
        am_columns.append({
            "title_accent": "Outdoor", "title_rest": "Amenities", "kicker": "Within the masterplan",
            "hero": out_hero, "duo": out_duo, "items": outdoor,
        })
    if indoor or in_hero:
        am_columns.append({
            "title_accent": "Indoor", "title_rest": "Amenities", "kicker": "Inside the residences",
            "hero": in_hero, "duo": in_duo, "items": indoor,
        })

    # ---- feature strip (page 3): only facts we actually hold ----
    features: list[dict] = []
    for icon, hints in _FEATURE_ICON_RULES:
        if len(features) >= 3:
            break
        match = next((n for n in amenity_names if any(h in n.lower() for h in hints)), None)
        if match and all(f["label"] != match for f in features):
            features.append({"icon": icon, "label": match})
    if project.furnishing:
        features.append({"icon": "furnishing", "label": clean_text(project.furnishing).replace("_", " ").title()})
    if handover:
        features.append({"icon": "handover", "label": f"Handover {handover}"})
    all_views = sorted({v for g in unit_groups for v in g["views"] if v})
    if all_views and len(features) < 5:
        features.append({"icon": "view", "label": all_views[0]})
    features = features[:5]

    # ---- investment numbers (computed + overrides; '—' otherwise) ----
    # Entry price = the cheapest available figure across the unit groups AND the
    # project's own min_price (covers units with NULL bedrooms that never grouped).
    entry_price = _f(ov.get("entry_price_aed"))
    cheapest_group = min((g for g in unit_groups if g["price_from"]),
                         key=lambda g: g["price_from"], default=None)
    if not entry_price:
        candidates = [g["price_from"] for g in unit_groups if g["price_from"]]
        mp = _f(project.min_price)
        if mp and mp > 0:
            candidates.append(mp)
        entry_price = min(candidates) if candidates else None

    # Price/sqft must pair a price and a size that describe the SAME unit. Prefer
    # the cheapest group's own size; fall back to project min_size only when the
    # entry price also came from the project row (not an override or a group).
    min_size_sqft = _size_to_sqft(project.min_size, project.area_unit)
    price_sqft = _f(ov.get("price_per_sqft_aed"))
    if not price_sqft:
        if cheapest_group and cheapest_group["price_from"] and cheapest_group["size_from"] \
                and entry_price == cheapest_group["price_from"]:
            price_sqft = cheapest_group["price_from"] / cheapest_group["size_from"]
        else:
            mp = _f(project.min_price)
            if min_size_sqft and mp and mp > 0 and entry_price == mp:
                price_sqft = mp / min_size_sqft

    cheaper_pct = _f(ov.get("cheaper_than_area_pct"))
    if cheaper_pct is None and price_sqft:
        med = await _district_median_price_sqft(db, project)
        if med:
            cheaper_pct = (price_sqft - med) / med * 100.0

    # Area-model investment estimates (net yield, area rent return, appreciation,
    # Y5 value, time-to-sell). Real area data feeds the formulas where we have it;
    # the per-community table is the fallback. Agent-stated overrides still win.
    m = None
    if entry_price:
        from_entry_group = bool(
            cheapest_group and cheapest_group["price_from"]
            and entry_price == cheapest_group["price_from"]
        )
        area_inputs = await metrics.gather_area_inputs(project)
        m = metrics.compute_metrics(
            entry_price,
            beds=cheapest_group["bedrooms"] if from_entry_group else None,
            sqft=cheapest_group["size_from"] if from_entry_group else None,
            community=district or city,
            area_yield=area_inputs["area_yield"],
            area_appreciation=area_inputs["area_appreciation"],
            activity_label=area_inputs["activity_label"],
        )

    def _metric(ov_key: str, m_key: str) -> Optional[float]:
        v = _f(ov.get(ov_key))
        if v is None and m is not None:
            v = _f(m.get(m_key))
        return v

    net_yield = _metric("net_yield_pct", "net_yield_pct")
    area_rent = _metric("area_avg_rent_pct", "area_avg_rent_return_pct")
    appreciation = _metric("annual_appreciation_pct", "annual_appreciation_pct")
    y5_value = _f(ov.get("y5_projected_value_aed"))
    if y5_value is None and appreciation is not None and entry_price:
        y5_value = entry_price * (1 + appreciation / 100.0) ** 5
    dom = _f(ov.get("days_on_market"))  # per-listing DOM — override-only, we don't store it
    tts = _metric("time_to_sell_days", "time_to_sell_days")

    service_charge = clean_text(ov.get("service_charge") or project.service_charge or "")
    sc_value, sc_unit = None, None
    if service_charge:
        m = re.search(r"(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?", service_charge)
        if m:
            sc_value = m.group(1) + (f"–{m.group(2)}" if m.group(2) else "")
            sc_unit = "AED"

    # Golden Visa: "Yes" only if EVERY unit clears AED 2M; if just the upper end of
    # the range qualifies, say so; "No" only when the whole range is below.
    top_price = max((p for p in (_f(project.max_price), entry_price) if p), default=None)
    if entry_price and entry_price >= GOLDEN_VISA_AED:
        golden = "Yes (≥ AED 2M)"
    elif top_price and top_price >= GOLDEN_VISA_AED:
        golden = "Select units"
    elif entry_price and _f(project.max_price):
        golden = "No"
    else:
        golden = None

    def row(k: str, v: Optional[str], unit: Optional[str] = None, style: Optional[str] = None) -> dict:
        if v is None:
            return {"k": k, "v": "—", "unit": None, "style": "dim"}
        return {"k": k, "v": v, "unit": unit, "style": style}

    # The cover field is labelled "Cheaper than Area Average": a project priced
    # BELOW the district median (cheaper_pct < 0) is the on-message case and prints
    # the discount. When it's actually a premium, relabel so the sign never lies.
    if cheaper_pct is None:
        cheaper_label, cheaper_val = "Cheaper than Area Average", None
    elif cheaper_pct <= 0:
        cheaper_label, cheaper_val = "Cheaper than Area Average", fmt_pct(cheaper_pct, signed=True)
    else:
        cheaper_label, cheaper_val = "Premium to Area Average", fmt_pct(cheaper_pct, signed=True)

    entry_compact = fmt_aed_compact(entry_price)
    numbers = [
        row("Entry Price", entry_compact.replace("AED ", "") if entry_compact else None, "AED"),
        row("Price / sqft", fmt_int(price_sqft), "AED"),
        row(cheaper_label, cheaper_val),
        row("Net Yield", fmt_pct(net_yield), style="warn"),
        row("Area Average Rent Return", fmt_pct(area_rent)),
        row("Annual Appreciation", fmt_pct(appreciation, signed=True) if appreciation is not None else None),
        row("Y5 Projected Value", (fmt_aed_compact(y5_value) or "").replace("AED ", "") or None, "AED" if y5_value else None),
        row("Service Charge / sqft", sc_value, sc_unit),
        row("Golden Visa Eligible", golden),
        row("Days on Market", fmt_int(dom)),
        row("Time to Sell in Area", f"{fmt_int(tts)} days" if tts else None),
        row("Anticipated Handover", handover),
    ]

    # ---- location ----
    lat, lng = _f(project.lat), _f(project.lng)
    drive_times, map_pois = [], []
    if lat and lng:
        ranked = sorted(
            ({"name": n, "ll": [la, lo], "km": _haversine_km(lat, lng, la, lo)} for n, la, lo in LANDMARKS),
            key=lambda d: d["km"],
        )
        drive_times = [{"name": d["name"], "mins": _drive_minutes(d["km"])} for d in ranked[:8]]
        map_pois = [{"name": d["name"], "ll": d["ll"]} for d in ranked[:5]]

    overview = project.description or (project.raw or {}).get("overview") or ""
    # The cover tagline is set in letter-spaced caps — only show a complete short
    # line, never a truncated sentence.
    tagline = clean_text(ov.get("tagline") or project.short_description or "")
    if not tagline and overview:
        first = _first_sentences(overview, 120)
        if len(first) <= 64 and not first.endswith("…"):
            tagline = first
    if len(tagline) > 80:
        tagline = ""
    place_label = " · ".join(x for x in (district, city) if x)

    bed_labels = [_bedrooms_label(g["bedrooms"])[1].replace(" BR", "") for g in unit_groups[:4]]
    bed_phrase = ", ".join(bed_labels[:-1]) + (" and " + bed_labels[-1] if len(bed_labels) > 1 else (bed_labels[0] if bed_labels else ""))

    pp = _payment_plan(project, handover)

    pricing_intro_bits = []
    if entry_compact:
        pricing_intro_bits.append(f"Starting prices from {entry_compact}")
    if pp:
        pricing_intro_bits.append(f"on a {pp['summary'].split(' · ')[0].replace(' / ', '/')} payment plan")
    if handover:
        pricing_intro_bits.append(f"anticipated handover {handover}")
    pricing_intro = (" · ".join(pricing_intro_bits) + ".") if pricing_intro_bits else None

    units_count = project.units_count
    # Page 3 intro: use the sentence AFTER the one page 2 leads with, so the two
    # pages don't repeat each other.
    gallery_intro = None
    if overview:
        stripped = _strip_markdown(overview)
        lead = _first_sentences(overview, 220)
        rest = stripped[len(lead):].strip()
        gallery_intro = _first_sentences(rest, 160) if len(rest) > 40 else None
    if not gallery_intro:
        dev_name = developer.name if developer else None
        gallery_intro = (f"Interiors and exteriors of {project.name}"
                         + (f", by {dev_name}." if dev_name else "."))

    context = {
        "project": {"name": project.name, "developer": developer.name if developer else None},
        "accent": "#8F7748",
        "cover": {
            "image": cover_photo["src"] if cover_photo else None,
            "kicker": place_label or city,
            "tagline": tagline or None,
        },
        "numbers": numbers,
        "location": {
            "intro": f"{project.name} sits in {district or city}, minutes from the city's landmark addresses, malls and airports." if (district or city) else None,
            "lead": _first_sentences(overview, 220) if overview else None,
            "drive_times": drive_times,
            "map_label": place_label or project.name,
            "lat": lat or 25.2048,
            "lng": lng or 55.2708,
            "map_pois": map_pois,
        },
        "gallery": {"intro": gallery_intro, "photos": gallery_photos},
        "features": features,
        "plans": {
            "intro": (f"Thoughtfully planned {bed_phrase} bedroom layouts prioritising space, light and comfort." if bed_phrase else "Floor plans supplied by the developer; further drawings on request."),
            "cards": plan_cards,
        },
        "amenities": {"columns": am_columns},
        "pricing": {
            "intro": pricing_intro or (f"A collection of {units_count} residences." if units_count else None),
            "rows": pricing_rows or [{
                "name": "Residences", "sub": None, "size": fmt_int(min_size_sqft) or "—",
                "size_unit": area_unit_label, "config": place_label or city,
                "price": entry_compact or "On request",
            }],
        },
        "payment_plan": pp,
        "agent": agent,
        "has_map": bool(lat and lng),
    }
    return context, files
