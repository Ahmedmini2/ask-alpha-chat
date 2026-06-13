"""generate_mini_brochure — the branded 6-page mini PDF brochure.

Triggered by "Branded PDF", "Mini PDF", "mini brochure", etc. Assembles project
data from the DB (photos classified by the vision model, payment plan from the
developer feed, computed metrics + agent-supplied overrides), renders the
Allegiance-branded template to PDF with headless Chromium, uploads it to S3,
and — on Telegram — sends the file straight into the chat.
"""
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.brochures import data as brochure_data
from app.brochures import render as brochure_render
from app.brochures import storage as brochure_storage
from app.config import settings
from app.core.profiles import get_profile, is_agent
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.brochures")

ASSETS_BUCKET = "assets-allegiance"

OVERRIDE_KEYS = (
    "net_yield_pct", "area_avg_rent_pct", "annual_appreciation_pct",
    "y5_projected_value_aed", "days_on_market", "time_to_sell_days",
    "cheaper_than_area_pct", "entry_price_aed", "price_per_sqft_aed",
    "service_charge", "tagline",
)


def _agent_block(profile) -> dict:
    first = (profile.first_name or "").strip()
    last = (profile.last_name or "").strip()
    name = f"{first} {last}".strip() or "Allegiance Advisory"
    initials = "".join(p[0] for p in name.split()[:2]).upper() or "A"
    contact_lines = []
    if profile.phone:
        contact_lines.append(profile.phone)
    if profile.email:
        contact_lines.append(profile.email)
    return {"name": name, "initials": initials, "contact_lines": contact_lines}


async def _send_telegram_document(chat_id: int, pdf_bytes: bytes, filename: str, caption: str) -> bool:
    if not settings.telegram_bot_token:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument"
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                url,
                data={"chat_id": str(chat_id), "caption": caption[:1024]},
                files={"document": (filename, pdf_bytes, "application/pdf")},
            )
            if r.status_code >= 400:
                log.warning("telegram sendDocument failed %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        log.warning("telegram sendDocument error: %s", e)
        return False


async def generate_mini_brochure_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. Brochure generation is only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    project = await brochure_data.resolve_project(db, args.get("project_id"), args.get("project_name"))
    if project is None:
        ref = args.get("project_name") or args.get("project_id")
        return {"error": f"No project found for {ref!r}. Search projects first to confirm the name."}

    overrides = {k: args[k] for k in OVERRIDE_KEYS if args.get(k) is not None}

    try:
        context, image_files = await brochure_data.build_context(
            db, project, agent=_agent_block(profile), overrides=overrides,
        )
        pdf_bytes = await brochure_render.render_brochure_pdf(context, image_files)
    except Exception as e:
        log.exception("brochure generation failed for project %s", project.id)
        return {"error": f"Brochure generation failed: {e}"}

    # Upload for a shareable link; Telegram delivery below works even if this
    # fails (e.g. missing s3:PutObject), so don't abort on it.
    s3_key, pdf_url = None, None
    try:
        s3_key, pdf_url = await brochure_storage.upload_pdf(pdf_bytes, project.name, ASSETS_BUCKET)
    except Exception as e:
        log.error("brochure S3 upload failed (continuing with Telegram only): %s", e)

    filename = f"{brochure_storage.slugify(project.name)}-mini-brochure.pdf"
    delivered = False
    tg_chat_id = ctx.get("telegram_chat_id")
    if tg_chat_id:
        delivered = await _send_telegram_document(
            int(tg_chat_id), pdf_bytes, filename,
            caption=f"📄 {project.name} — mini brochure",
        )

    if not delivered and not pdf_url:
        return {"error": "Brochure was rendered but could not be delivered: S3 upload "
                         "failed and no Telegram chat is linked. Ask an admin to grant "
                         "s3:PutObject on the assets bucket."}

    filled = sum(1 for n in context["numbers"] if n["v"] != "—")
    missing = [n["k"] for n in context["numbers"] if n["v"] == "—"]
    log.info("brochure ready project=%s size=%dKB url=%s telegram=%s",
             project.id, len(pdf_bytes) // 1024, s3_key, delivered)
    result = {
        "status": "completed",
        "project_id": project.id,
        "project_name": project.name,
        "pdf_url": pdf_url,
        "filename": filename,
        "pages": 6,
        "sent_to_telegram": delivered,
        "metrics_filled": filled,
        "metrics_missing": missing,
    }
    if pdf_url:
        result["url_expires"] = "7 days"
    else:
        result["note"] = ("No download link — S3 upload unavailable. The PDF was "
                          "delivered via Telegram only.")
    return result


registry.register(Tool(
    name="generate_mini_brochure",
    description=(
        "Generate the Allegiance-branded 6-page mini PDF brochure for an off-plan project "
        "(cover with investment numbers, location map with drive times, photo gallery, floor "
        "plans, amenities, pricing and payment plan). Use when an agent asks for a 'Branded "
        "PDF', 'Mini PDF', 'mini brochure', 'project PDF' or similar. Agents only. Takes "
        "~30-60 seconds. Returns a download URL; on Telegram the PDF file is also sent "
        "directly into the chat. Investment metrics (net yield, area rent return, appreciation, "
        "Y5 value, time-to-sell) are auto-filled from our area model — agents can still override "
        "any of them by stating a value (pass it as the matching override argument). Days on market "
        "is the only one that stays blank unless the agent provides it. Never invent override values "
        "yourself — only pass numbers the agent explicitly stated."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID (preferred — get it from search_projects)."},
            "project_name": {"type": "string", "description": "Project name if the ID is unknown."},
            "net_yield_pct": {"type": "number", "description": "Net rental yield %, ONLY if the agent stated it."},
            "area_avg_rent_pct": {"type": "number", "description": "Area average rent return %, ONLY if the agent stated it."},
            "annual_appreciation_pct": {"type": "number", "description": "Annual appreciation %, ONLY if the agent stated it."},
            "y5_projected_value_aed": {"type": "number", "description": "Year-5 projected value in AED, ONLY if the agent stated it (otherwise computed from appreciation when available)."},
            "days_on_market": {"type": "number", "description": "Days on market, ONLY if the agent stated it."},
            "time_to_sell_days": {"type": "number", "description": "Typical days to sell in the area, ONLY if the agent stated it."},
            "cheaper_than_area_pct": {"type": "number", "description": "Discount vs area average %, ONLY if the agent stated it (negative = cheaper)."},
            "entry_price_aed": {"type": "number", "description": "Override entry price in AED, ONLY if the agent stated it."},
            "price_per_sqft_aed": {"type": "number", "description": "Override price per sqft in AED, ONLY if the agent stated it."},
            "service_charge": {"type": "string", "description": "Override service charge text, e.g. '18 AED/sqft'."},
            "tagline": {"type": "string", "description": "Optional one-line tagline for the cover."},
        },
    },
    handler=generate_mini_brochure_handler,
))
