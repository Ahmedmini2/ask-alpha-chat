"""generate_branding_image — personal-branding posters for agents (Nano Banana Pro).

Flow (driven by the model via the system prompt; this tool just does each step):
  1. action="list_templates"  → return the ~12 sample templates (id, title, thumbnail) to choose from.
  2. agent picks one → ask if they want a short text overlay.
  3. action="generate" with template_id (+ overlay_text, or add_text=false for a clean image).

We take the agent's own profile photo (profiles.avatar_key, stored in our assets S3 bucket),
hand it + the chosen template image to Gemini "Nano Banana Pro", and get back a poster that
restyles the agent into the template (face preserved) with the optional headline. The result is
uploaded to S3 (presigned link) and, on Telegram, pushed inline; each generation is recorded in
the agent's gallery (branding_images).
"""
import asyncio
import logging
import mimetypes
import uuid
from datetime import datetime, timezone

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.branding import templates as tmpl
from app.brochures import storage as brochure_storage
from app.config import settings
from app.core.profiles import get_profile, is_agent
from app.db.models import BrandingImage
from app.db.session import AsyncSessionLocal
from app.integrations import gemini_images
from app.tools.flyers import _send_telegram_photo
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.branding")

ASSETS_BUCKET = brochure_storage.ASSETS_BUCKET


def _templates_payload(thumbs: dict[str, str | None]) -> list[dict]:
    return [
        {
            "id": t.slug,
            "title": t.title,
            "description": t.description,
            "suggested_text": t.suggested_text,
            "aspect_ratio": t.aspect_ratio,
            "thumbnail_url": thumbs.get(t.slug),
        }
        for t in tmpl.all_templates()
    ]


async def _build_thumbnails() -> dict[str, str | None]:
    """Presigned thumbnail URL per template (self-seeds the bundled JPEG into S3 if missing).
    Best-effort: a template with no URL still shows by title/description."""
    async def one(t: tmpl.BrandingTemplate) -> tuple[str, str | None]:
        try:
            data = t.read_bytes()
        except Exception:
            return t.slug, None
        url = await brochure_storage.ensure_and_presign(ASSETS_BUCKET, t.s3_key, data, "image/jpeg")
        return t.slug, url

    pairs = await asyncio.gather(*[one(t) for t in tmpl.all_templates()])
    return dict(pairs)


def _gemini_error_message(e: gemini_images.GeminiImageError) -> str:
    if e.kind == "quota":
        return ("Image generation hit Google's quota. The Nano Banana image models have no free "
                "tier — an admin needs to enable billing on the Gemini (Google AI Studio) project "
                "for our API key. Once that's done, try again.")
    if e.kind == "config":
        return ("Image generation isn't configured yet — the Gemini API key is missing. Ask an "
                "admin to set GOOGLE_AI_STUDIO_API_KEY.")
    if e.kind == "blocked":
        return ("That image couldn't be generated from this photo and template (it was blocked by "
                "a safety filter). Try a different template, or a clearer head-and-shoulders photo.")
    if e.kind == "overloaded":
        return ("Google's image service is busy right now — I tried the backup model too, but both "
                "came back overloaded. This is on Google's side and usually clears within a few "
                "minutes, so please try again shortly.")
    return "Image generation failed this time — please try again in a moment."


async def _persist(user_id, slug: str, overlay: str | None, s3_key: str | None, image_url: str | None) -> None:
    """Record the generation in the agent's gallery. Isolated session + best-effort: if the
    branding_images table isn't migrated yet, log and move on without affecting the chat turn."""
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(insert(BrandingImage).values(
                id=uuid.uuid4(),
                user_id=user_id,
                template_slug=slug,
                overlay_text=overlay,
                s3_key=s3_key,
                image_url=image_url,
                status="completed",
                created_at=datetime.now(timezone.utc),
            ))
            await s.commit()
    except Exception as e:
        log.warning("branding gallery persist skipped (table not migrated?): %s", e)


async def _recent_history(user_id, limit: int = 12) -> list[dict]:
    """Recent generations for this agent (isolated session, best-effort)."""
    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(
                select(BrandingImage)
                .where(BrandingImage.user_id == user_id)
                .order_by(BrandingImage.created_at.desc())
                .limit(limit)
            )).scalars().all()
        out = []
        for r in rows:
            t = tmpl.get_template(r.template_slug)
            out.append({
                "id": str(r.id),
                "template_id": r.template_slug,
                "template_title": t.title if t else r.template_slug,
                "overlay_text": r.overlay_text,
                "image_url": r.image_url,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return out
    except Exception as e:
        log.warning("branding history read skipped (table not migrated?): %s", e)
        return []


async def generate_branding_image_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. Personal-branding images are only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}
    if not settings.branding_images_enabled:
        return {"error": "Personal-branding image generation is currently turned off."}
    if not gemini_images.configured():
        return {"error": "Image generation isn't configured yet — the Gemini API key (GOOGLE_AI_STUDIO_API_KEY) "
                         "is missing. Ask an admin to set it."}

    action = (args.get("action") or "").strip().lower()
    template = tmpl.get_template(args.get("template_id"))

    if action == "list_history":
        images = await _recent_history(user_id)
        return {"status": "history", "count": len(images), "images": images}

    # Show the menu when explicitly asked, or whenever we're not generating and no template is set.
    if action == "list_templates" or (action != "generate" and template is None):
        thumbs = await _build_thumbnails()
        return {"status": "templates", "count": len(tmpl.all_templates()),
                "templates": _templates_payload(thumbs)}

    # action == "generate" from here.
    if template is None:
        thumbs = await _build_thumbnails()
        return {"status": "needs_template",
                "message": "That template id wasn't recognised — ask the agent to pick one of the templates below.",
                "templates": _templates_payload(thumbs)}

    add_text = bool(args.get("add_text"))
    overlay = (args.get("overlay_text") or "").strip()
    if overlay:
        add_text = True
    if add_text and not overlay:
        return {"status": "needs_text", "template_id": template.slug, "template_title": template.title,
                "suggested_text": template.suggested_text,
                "message": (f"The agent wants text on the image. Ask them for the short line to put on it "
                            f"(keep it under {tmpl.MAX_OVERLAY_CHARS} characters / ~6 words). You can offer "
                            f"the template's default line as a suggestion: \"{template.suggested_text}\".")}
    if overlay and len(overlay) > tmpl.MAX_OVERLAY_CHARS:
        return {"status": "text_too_long", "template_id": template.slug, "max_chars": tmpl.MAX_OVERLAY_CHARS,
                "message": (f"That overlay text is {len(overlay)} characters — too long for a clean poster. "
                            f"Ask the agent to shorten it to under {tmpl.MAX_OVERLAY_CHARS} characters "
                            "(a short, punchy line works best).")}

    if not profile.avatar_key:
        return {"error": "You don't have a profile picture set yet. Add a profile photo in your Ask Alpha "
                         "settings, then I can generate your branding image."}
    profile_bytes = await brochure_storage.fetch_asset_bytes(ASSETS_BUCKET, profile.avatar_key)
    if not profile_bytes:
        return {"error": "I couldn't load your profile picture from storage. Please re-upload it in your "
                         "settings and try again."}
    profile_mime = mimetypes.guess_type(profile.avatar_key)[0] or "image/jpeg"

    try:
        template_bytes = template.read_bytes()
    except Exception as e:
        log.error("template bytes read failed %s: %s", template.slug, e)
        return {"error": "That template is temporarily unavailable — please pick another one."}

    prompt = tmpl.build_prompt(template, overlay or None)
    try:
        png = await gemini_images.generate_branding_image(
            template_bytes, profile_bytes, prompt,
            aspect_ratio=template.aspect_ratio, profile_mime=profile_mime,
        )
    except gemini_images.GeminiImageError as e:
        log.warning("branding gen failed user=%s template=%s kind=%s: %s", user_id, template.slug, e.kind, e)
        return {"error": _gemini_error_message(e)}

    base = f"branding-{template.slug}-{str(user_id)[:8]}"
    s3_key, image_url = None, None
    try:
        s3_key, image_url = await brochure_storage.upload_png(png, base, ASSETS_BUCKET)
    except Exception as e:
        log.error("branding S3 upload failed (continuing): %s", e)

    filename = f"{base}.png"
    delivered = False
    tg_chat_id = ctx.get("telegram_chat_id")
    if tg_chat_id:
        delivered = await _send_telegram_photo(
            int(tg_chat_id), png, filename,
            caption=f"🎨 Your branding image — {template.title}",
        )

    if not delivered and not image_url:
        return {"error": "Your image was generated but couldn't be delivered this time (both the download "
                         "link and Telegram failed). Please try again."}

    await _persist(user_id, template.slug, overlay or None, s3_key, image_url)

    log.info("branding image ready user=%s template=%s text=%s size=%dKB url=%s telegram=%s",
             user_id, template.slug, bool(overlay), len(png) // 1024, s3_key, delivered)
    result = {
        "status": "completed",
        "template_id": template.slug,
        "template_title": template.title,
        "has_text": bool(overlay),
        "overlay_text": overlay or None,
        "image_url": image_url,
        "filename": filename,
        "sent_to_telegram": delivered,
    }
    if image_url:
        result["url_expires"] = "7 days"
    else:
        result["note"] = "No download link — S3 upload unavailable. Delivered via Telegram only."
    return result


registry.register(Tool(
    name="generate_branding_image",
    description=(
        "Generate a personal-branding poster for the signed-in AGENT: their own profile photo, "
        "restyled into one of ~12 curated editorial templates (with their face preserved) using "
        "Google Nano Banana Pro, plus an OPTIONAL short headline overlay. Agents only — anonymous "
        "users must sign in. THREE-STEP FLOW: (1) call with action='list_templates' to show the "
        "samples; let the agent pick one. (2) Ask whether they want a short text overlay; if yes, "
        "get the exact short line (<=60 characters / ~6 words). (3) Call with action='generate', "
        "template_id=<chosen id>, and either overlay_text=<their line> OR add_text=false for a "
        "clean, text-free image. The call is synchronous (~15-40s); after it returns, tell them "
        "the image is ready but do NOT paste the download URL yourself — the system attaches the "
        "exact link automatically. On Telegram the image is also sent into the chat. Use "
        "action='list_history' to show the agent their past branding images. If the tool returns "
        "status='needs_text' or 'text_too_long', relay its message and ask the agent accordingly; "
        "if status='needs_template'/'templates', show the templates and ask them to choose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_templates", "generate", "list_history"],
                "description": ("'list_templates' = show the sample templates to choose from. "
                                "'generate' = produce the image (requires template_id). "
                                "'list_history' = show the agent's previously generated images."),
            },
            "template_id": {
                "type": "string",
                "description": "The chosen template's id (slug), e.g. 'no-days-off'. Required for action='generate'.",
            },
            "add_text": {
                "type": "boolean",
                "description": ("Whether the agent wants a text overlay. Set true only if they said yes. "
                                "If true, also pass overlay_text. If false (or omitted), a clean, text-free "
                                "image is produced."),
            },
            "overlay_text": {
                "type": "string",
                "description": ("The exact short headline to render on the image (<=60 chars / ~6 words). "
                                "Only set when the agent wants text. Pass their words verbatim."),
            },
        },
        "required": ["action"],
    },
    handler=generate_branding_image_handler,
))
