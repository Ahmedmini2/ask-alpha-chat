"""generate_whatsapp_flyer — a single shareable PNG flyer for a project.

Two variants, both portrait images built from the same design system as the mini
brochure:
  * key_facts  — starting price, payment plan, handover, location (a 2x2 board)
  * investment — the "Numbers at a Glance" investment summary (12 metrics)

Triggered by "WhatsApp flyer", "flyer", "give me an image of the key facts /
investment insights", etc. Renders headless to PNG, uploads it to S3, and — on
Telegram — pushes the image straight into the chat (sendPhoto, so it previews
inline like a forwarded WhatsApp graphic).
"""
import asyncio
import io
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.brochures import flyer as flyer_data
from app.brochures import render as brochure_render
from app.brochures import storage as brochure_storage
from app.brochures.data import resolve_project
from app.config import settings
from app.core.profiles import get_profile, is_agent
from app.tools.brochures import ASSETS_BUCKET, _send_telegram_document
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.flyers")

# Investment-flyer overrides the agent may state in chat (mirrors the brochure).
OVERRIDE_KEYS = (
    "net_yield_pct", "area_avg_rent_pct", "annual_appreciation_pct",
    "y5_projected_value_aed", "days_on_market", "time_to_sell_days",
    "cheaper_than_area_pct", "entry_price_aed", "price_per_sqft_aed",
    "service_charge",
)

_TYPE_LABEL = {"key_facts": "Key Facts", "investment": "Investment Insights"}


async def _send_telegram_photo(chat_id: int, png_bytes: bytes, filename: str, caption: str) -> bool:
    """Push the flyer as a photo so it previews inline in the chat. Telegram
    re-encodes sendPhoto to JPEG; if it rejects the image (e.g. too large) we fall
    back to sendDocument, which keeps the crisp PNG as a downloadable file."""
    if not settings.telegram_bot_token:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    timeout = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0)
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(
                    url,
                    data={"chat_id": str(chat_id), "caption": caption[:1024]},
                    files={"photo": (filename, png_bytes, "image/png")},
                )
            if r.status_code < 400:
                return True
            log.warning("telegram sendPhoto failed %s: %s", r.status_code, r.text[:300])
            if r.status_code != 429:
                break
        except Exception as e:
            log.warning("telegram sendPhoto error (attempt %d/2): %r", attempt + 1, e)
        await asyncio.sleep(1.5 * (attempt + 1))
    # Fall back to a document so the agent still gets the file.
    return await _send_telegram_document(
        chat_id, png_bytes, filename, caption, mime_type="image/png",
    )


async def generate_whatsapp_flyer_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. Flyer generation is only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    project = await resolve_project(db, args.get("project_id"), args.get("project_name"))
    if project is None:
        ref = args.get("project_name") or args.get("project_id")
        return {"error": f"No project found for {ref!r}. Search projects first to confirm the name."}

    flyer_type = flyer_data._normalize_flyer_type(args.get("flyer_type"))
    overrides = {k: args[k] for k in OVERRIDE_KEYS if args.get(k) is not None}

    try:
        context, image_files = await flyer_data.build_flyer_context(
            db, project, flyer_type, overrides=overrides,
        )
        png_bytes = await brochure_render.render_flyer_png(context, image_files)
    except Exception as e:
        log.exception("flyer generation failed for project %s", project.id)
        return {"error": f"Flyer generation failed: {e}"}

    label = _TYPE_LABEL[flyer_type]
    base = f"{brochure_storage.slugify(project.name)}-{flyer_type}-flyer"

    s3_key, image_url = None, None
    try:
        s3_key, image_url = await brochure_storage.upload_png(png_bytes, base, ASSETS_BUCKET)
    except Exception as e:
        log.error("flyer S3 upload failed (continuing with Telegram only): %s", e)

    filename = f"{base}.png"
    delivered = False
    tg_chat_id = ctx.get("telegram_chat_id")
    if tg_chat_id:
        delivered = await _send_telegram_photo(
            int(tg_chat_id), png_bytes, filename,
            caption=f"📸 {project.name} — {label}",
        )

    if not delivered and not image_url:
        why = ("Telegram delivery failed" if tg_chat_id else "no Telegram chat is linked")
        return {"error": "Flyer was rendered but could not be delivered: S3 upload was "
                         f"denied and {why}. The S3 download link needs an admin to grant "
                         "s3:PutObject on the assets bucket; until then delivery relies on "
                         "Telegram. Please try again."}

    log.info("flyer ready project=%s type=%s size=%dKB url=%s telegram=%s",
             project.id, flyer_type, len(png_bytes) // 1024, s3_key, delivered)
    result = {
        "status": "completed",
        "project_id": project.id,
        "project_name": project.name,
        "flyer_type": flyer_type,
        "flyer_label": label,
        "image_url": image_url,
        "filename": filename,
        "sent_to_telegram": delivered,
    }
    if image_url:
        result["url_expires"] = "7 days"
    else:
        result["note"] = ("No download link — S3 upload unavailable. The flyer was "
                          "delivered via Telegram only.")
    return result


registry.register(Tool(
    name="generate_whatsapp_flyer",
    description=(
        "Generate a single shareable PNG flyer (portrait, WhatsApp/social-ready) for a project, "
        "using the Allegiance brand design. Two variants: 'key_facts' (starting price, payment "
        "plan, handover, location) and 'investment' (the 'Numbers at a Glance' investment summary "
        "— entry price, price/sqft, net yield, appreciation, Y5 value, golden visa, time to sell, "
        "handover, etc.). Use when an agent asks for a 'WhatsApp flyer', 'flyer', 'social image', "
        "or 'an image of the key facts / investment insights' for a project. Agents only. Resolve "
        "the project first (search_projects) and pass project_id (preferred) or project_name, plus "
        "flyer_type. Returns a download URL; on Telegram the image is also sent into the chat. "
        "Investment metrics are auto-filled from our area model — the agent can override any of them "
        "by stating a value (pass it as the matching argument). NEVER invent override values."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID (preferred — from search_projects)."},
            "project_name": {"type": "string", "description": "Project name if the ID is unknown."},
            "flyer_type": {
                "type": "string",
                "enum": ["key_facts", "investment"],
                "description": (
                    "Which flyer to make. 'key_facts' = starting price / payment plan / handover / "
                    "location. 'investment' = the Numbers at a Glance investment summary. Map user "
                    "words: 'investment insights'/'numbers'/'yields' -> 'investment'; anything else "
                    "(or a bare 'flyer') -> 'key_facts'."
                ),
            },
            "net_yield_pct": {"type": "number", "description": "Net rental yield %, ONLY if the agent stated it (investment flyer)."},
            "area_avg_rent_pct": {"type": "number", "description": "Area average rent return %, ONLY if the agent stated it."},
            "annual_appreciation_pct": {"type": "number", "description": "Annual appreciation %, ONLY if the agent stated it."},
            "y5_projected_value_aed": {"type": "number", "description": "Year-5 projected value in AED, ONLY if the agent stated it."},
            "days_on_market": {"type": "number", "description": "Days on market, ONLY if the agent stated it."},
            "time_to_sell_days": {"type": "number", "description": "Typical days to sell in the area, ONLY if the agent stated it."},
            "cheaper_than_area_pct": {"type": "number", "description": "Discount vs area average %, ONLY if the agent stated it (negative = cheaper)."},
            "entry_price_aed": {"type": "number", "description": "Override entry/starting price in AED, ONLY if the agent stated it."},
            "price_per_sqft_aed": {"type": "number", "description": "Override price per sqft in AED, ONLY if the agent stated it."},
            "service_charge": {"type": "string", "description": "Override service charge text, e.g. '18 AED/sqft'."},
        },
    },
    handler=generate_whatsapp_flyer_handler,
))
