"""Assemble the template context for a single-image WhatsApp/social flyer.

Two variants, both rendered to a portrait PNG that reuses the mini-brochure's
design system (cover hero + Allegiance footer):

  * ``investment`` — the "Numbers at a Glance" investment summary (same 12 rows
    as the brochure cover, via the shared ``compute_financials``).
  * ``key_facts``  — a 2x2 board: starting price, payment plan, handover, location.

Everything factual comes from the database (or explicit agent overrides); numbers
we can neither compute nor were given render as "—" — the flyer never invents data.
"""
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brochures import storage
from app.brochures.data import (
    _f,
    _gather_units,
    _payment_plan,
    clean_text,
    compute_financials,
    fmt_quarter,
    handover_label,
)
from app.db.models import Developer, Project, ProjectAsset

log = logging.getLogger("askalpha.flyers")

FLYER_TYPES = ("key_facts", "investment")


def _normalize_flyer_type(raw: Optional[str]) -> str:
    """Map user phrasing onto the two canonical variants. Defaults to key_facts."""
    key = (raw or "").strip().lower().replace("-", " ").replace("_", " ")
    if any(t in key for t in ("invest", "number", "glance", "yield", "roi", "metric", "summary")):
        return "investment"
    return "key_facts"


async def _cover_image(db: AsyncSession, project: Project) -> Optional[bytes]:
    """Lowest-position stored image asset — the project's primary render."""
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


def _quarter_long(q: Optional[str]) -> Optional[str]:
    """'2029-Q1' -> 'Q1 2029' (the flyer shows the full year, unlike the cover)."""
    import re
    if not q:
        return None
    m = re.match(r"(\d{4})-Q(\d)", q.strip())
    return f"Q{m.group(2)} {m.group(1)}" if m else q


def _bed_phrase(unit_groups: list[dict], project: Project) -> Optional[str]:
    """'Studio, 1 & 2 bedroom residences' from the available unit mix."""
    beds = sorted({int(b) for g in unit_groups if (b := _f(g.get("bedrooms"))) is not None})
    if not beds:
        n = project.units_count
        return f"{n} residences" if n else None
    labels = ["Studio" if b == 0 else str(b) for b in beds]
    if len(labels) == 1:
        joined = labels[0]
    else:
        joined = ", ".join(labels[:-1]) + " & " + labels[-1]
    has_bedroomed = any(b > 0 for b in beds)
    return f"{joined} bedroom residences" if has_bedroomed else f"{joined} residences"


def _payment_facts(project: Project, handover: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(headline, sub) e.g. ('40/60', '60% on completion'). None when no plan."""
    pp = _payment_plan(project, handover)
    if not pp:
        return None, None
    head = pp["summary"].split(" · ")[0].strip()       # '40 / 60'
    headline = head.replace(" / ", "/")
    final = head.split("/")[-1].strip() if "/" in head else None
    sub = f"{final}% on completion" if final else "Staged payment plan"
    return headline, sub


def _handover_facts(project: Project) -> tuple[str, str]:
    """(value, sub) for the flyer Handover cell, with the construction fallback for projects with
    no handover quarter: handover quarter -> handover/construction date -> construction progress."""
    ql = _quarter_long(project.completion_quarter)
    if ql:
        return ql, "Anticipated"
    for d in (project.completion_date, getattr(project, "construction_end_date", None)):
        if d:
            try:
                return f"Q{(d.month - 1) // 3 + 1} {d.year}", "Anticipated"
            except (AttributeError, TypeError):
                pass
    rp = getattr(project, "readiness_progress", None)
    if rp is not None:
        try:
            rp = float(rp)
        except (TypeError, ValueError):
            rp = None
        if rp is not None and rp >= 100:
            return "Ready", "Completed"
        if rp is not None and rp > 0:
            # Cap at 99 so an under-construction project never rounds up to a
            # contradictory '100% built' under the 'Under construction' sub-label.
            return f"{min(99, round(rp))}% built", "Under construction"
    return "TBA", "To be announced"


def _key_facts(project: Project, unit_groups: list[dict], fin: dict, handover: Optional[str]) -> list[dict]:
    district = clean_text(project.district)
    city = clean_text(project.city)
    # city is NULL for most rows — fall back to district, then a sensible default.
    loc_big = (district or city or "United Arab Emirates").upper()
    loc_sub = city if (city and city != district) else "Dubai"

    entry = fin.get("entry_compact")  # 'AED 4.08M'
    pay_head, pay_sub = _payment_facts(project, handover)
    hand_long, hand_sub = _handover_facts(project)

    return [
        {
            "label": "Starting Price",
            "unit": "AED" if entry else None,
            "value": entry.replace("AED ", "") if entry else "On request",
            "sub": _bed_phrase(unit_groups, project) or "Select residences",
        },
        {
            "label": "Payment Plan",
            "unit": None,
            "value": pay_head or "Flexible",
            "sub": pay_sub or "On request",
        },
        {
            "label": "Handover",
            "unit": None,
            "value": hand_long,
            "sub": hand_sub,
        },
        {
            "label": "Location",
            "unit": None,
            "value": loc_big,
            "sub": loc_sub,
        },
    ]


def _cover_kicker_tagline(project: Project) -> tuple[str, Optional[str]]:
    """Hero overlay: a short marketing kicker + the place line."""
    district = clean_text(project.district)
    city = clean_text(project.city)
    place = " · ".join(x for x in (district, city) if x) or city or "Dubai"
    tagline = clean_text(project.short_description or "")
    if len(tagline) > 80:
        tagline = ""
    # Kicker = the tagline when we have one (matches the reference), else the place.
    return (tagline.upper() if tagline else place.upper()), place


async def build_flyer_context(
    db: AsyncSession,
    project: Project,
    flyer_type: str,
    overrides: Optional[dict] = None,
) -> tuple[dict, dict[str, bytes]]:
    """Returns (template_context, image_files {relative_name: bytes})."""
    flyer_type = _normalize_flyer_type(flyer_type)
    ov = overrides or {}
    files: dict[str, bytes] = {}

    developer = None
    if project.developer_id:
        developer = (await db.execute(
            select(Developer).where(Developer.id == project.developer_id)
        )).scalar_one_or_none()

    district = clean_text(project.district)
    city = clean_text(project.city) or "Dubai"
    handover = handover_label(project)   # construction-date / progress fallback when no quarter

    unit_groups = await _gather_units(db, project)

    fin = await compute_financials(
        db, project, unit_groups,
        district=district, city=city, handover=handover, overrides=ov,
    )

    cover_bytes = await _cover_image(db, project)
    cover_name = None
    if cover_bytes:
        cover_name = "cover.jpg"
        files[cover_name] = cover_bytes

    kicker, _place = _cover_kicker_tagline(project)

    context = {
        "flyer_type": flyer_type,
        "accent": "#C2622C",  # terracotta eyebrow, matching the reference flyers
        "project": {"name": project.name, "developer": developer.name if developer else None},
        "cover": {"image": cover_name, "kicker": kicker},
        "numbers": fin["numbers"],
        "key_facts": _key_facts(project, unit_groups, fin, handover),
    }
    return context, files
