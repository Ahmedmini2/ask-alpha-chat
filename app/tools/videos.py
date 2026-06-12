import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select, insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.core.profiles import get_profile, is_agent
from app.db.models import Project, Video
from app.integrations import bedrock_images, heygen
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.videos")

_bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region)
# gpt-oss (OpenAI on Bedrock) for script writing may live in a different region than the
# main reasoning model; keep a dedicated client so the two are independently configurable.
_script_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=settings.bedrock_script_region or settings.aws_region,
)


CAMPAIGN_SCRIPT_SYSTEM_PROMPT = """You write short spoken-style UAE real-estate Reel/TikTok promo scripts \
(roughly 30–60 seconds, 120–180 words) in this exact three-beat structure, delivered as one continuous \
spoken paragraph the avatar reads verbatim — NO labels, NO markdown, NO headings, just the spoken words.

STRUCTURE (run them together, no visible breaks):

1. HOOK (1 sentence, ~15–25 words). Lead with the single most visual/enviable benefit. \
Do NOT open with the developer name. Avoid filler adjectives like "luxurious", "prime", \
"unparalleled" — they're invisible.

2. VALUE / CORE MESSAGE (2–4 sentences, ~70–100 words). Open with the developer-credibility move \
("X introduces its first-ever launch in…", "X's flagship development…"). Anchor location against a \
famous nearby landmark (Yas Island, Downtown Dubai, Palm Jumeirah, Dubai Marina, Burj Khalifa). Stack \
3–5 concrete numbers — total sqft, tree count, % green, amenities, distances. Use spoken connectives \
sparingly: "we're talking about…", "and here's the best part…", "now picture this…" (max one or two).

3. CTA (1–2 sentences, ~25–45 words). State the starting price (e.g., "AED 1.35 million") and the \
payment plan (e.g., "60/40 plan"). Frame the price as "the best entry point" rather than "affordable". \
End with the action: "Submit your EOI today to get priority access."

VOICE & RHYTHM:
- Spoken, NOT read. Contractions mandatory ("it's", "we're", "you're").
- Numbers must be speakable: "38 million square feet", "AED 1.35 million" — never "38,000,000".
- Short sentences. Breath units. Drop adjectives, keep nouns and numbers.
- Landmarks do the work of adjectives.
- End on urgency, never volume. "Submit your EOI today" beats "Don't miss out!"

HARD RULES:
- NEVER invent numbers, landmarks, distances, or stats. If a fact isn't in the brief, omit it.
- If starting price is missing, drop it from the CTA and end on "Submit your EOI today to get priority access."
- Stay within 180 words. Shorter is better.
- Output ONLY the spoken narration — no section labels, no asterisks, no quotes, no preamble."""


def _campaign_brief(project: Project) -> str:
    """Format the project's hard facts as a brief Claude can write from."""
    facts: list[str] = []
    if project.name:
        facts.append(f"Project: {project.name}")
    if project.developer and project.developer.name:
        facts.append(f"Developer: {project.developer.name}")
    loc = " ".join(p for p in (project.district, project.city, project.region, project.country) if p).strip()
    if loc:
        facts.append(f"Location: {loc}")
    if project.short_description:
        facts.append(f"Tagline: {project.short_description}")
    if project.description:
        facts.append(f"Description: {project.description[:800]}")
    if project.amenities:
        amen = project.amenities
        if isinstance(amen, list):
            names = []
            for a in amen[:12]:
                if isinstance(a, str):
                    names.append(a)
                elif isinstance(a, dict):
                    names.append(a.get("name") or a.get("title") or "")
            names = [n for n in names if n]
            if names:
                facts.append(f"Amenities: {', '.join(names)}")
    if project.units_count:
        facts.append(f"Total units: {project.units_count}")
    if project.units:
        beds = sorted({float(u.bedrooms) for u in project.units if u.bedrooms is not None})
        if beds:
            def _bed(n: float) -> str:
                return "studio" if n == 0 else (str(int(n)) if n == int(n) else str(n))
            rng = _bed(beds[0]) if beds[0] == beds[-1] else f"{_bed(beds[0])}–{_bed(beds[-1])}"
            facts.append(f"Bedrooms: {rng}")
    if project.furnishing:
        facts.append(f"Furnishing: {project.furnishing}")
    if project.service_charge:
        facts.append(f"Service charge: {project.service_charge}")
    if project.min_price and project.currency:
        facts.append(f"Starting price: {project.currency} {project.min_price:,.0f}")
    if project.completion_quarter:
        facts.append(f"Completion: {project.completion_quarter}")
    if project.sale_status:
        facts.append(f"Sale status: {project.sale_status}")
    if project.has_escrow is True:
        facts.append("Escrow protected: yes")
    if project.deposit_description:
        facts.append(f"Payment / deposit notes: {project.deposit_description}")
    if project.post_handover is True:
        facts.append("Has post-handover payment plan")
    return "\n".join(f"- {f}" for f in facts)


def _extract_text(resp: dict) -> str:
    """Pull the answer text out of a Converse response. gpt-oss is a reasoning model,
    so message.content may hold a `reasoningContent` block alongside the `text` block —
    concatenate only the text blocks and skip the chain-of-thought."""
    blocks = (resp.get("output", {}).get("message", {}) or {}).get("content", []) or []
    return "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b).strip()


async def _write_campaign_script(project: Project) -> str:
    brief = _campaign_brief(project)
    messages = [{
        "role": "user",
        "content": [{"text":
            "Write the spoken script from this brief. Only use facts present below; "
            "omit anything missing. Output the narration as one continuous paragraph "
            "with no labels.\n\n" + brief
        }],
    }]
    effort = (settings.bedrock_script_reasoning_effort or "").strip().lower()

    def _call() -> str:
        kwargs = dict(
            modelId=settings.bedrock_script_model_id,
            system=[{"text": CAMPAIGN_SCRIPT_SYSTEM_PROMPT}],
            messages=messages,
            # gpt-oss spends tokens on reasoning BEFORE the answer, so give generous
            # headroom beyond the ~180-word script. Temperature/topP are accepted on Bedrock.
            inferenceConfig={"maxTokens": 4096, "temperature": 0.7, "topP": 0.9},
        )

        def _run(extra: dict) -> str:
            resp = _script_bedrock.converse(**kwargs, **extra)
            text = _extract_text(resp)
            if not text and resp.get("stopReason") == "max_tokens":
                log.warning(
                    "gpt-oss script hit maxTokens with no answer text (reasoning starvation) model=%s",
                    settings.bedrock_script_model_id,
                )
            return text

        if effort:
            try:
                text = _run({"additionalModelRequestFields": {"reasoning_effort": effort}})
                if text:
                    return text
                # Succeeded but produced only reasoning (no answer) — retry without the
                # effort hint, which gives the answer the full token budget.
                log.warning("empty script with reasoning_effort=%s; retrying without it", effort)
            except ClientError as e:
                # Some models/regions reject reasoning_effort — retry without it rather
                # than fail the whole video.
                if "ValidationException" not in str(e) and "reasoning" not in str(e).lower():
                    raise
                log.warning("reasoning_effort rejected by %s; retrying plain", settings.bedrock_script_model_id)
        return _run({})

    return await asyncio.to_thread(_call)


def _compose_background_prompt(project: Project, user_prompt: str) -> str:
    """Build the text-to-image prompt for the 9:16 background plate that HeyGen composites
    the avatar in front of. Describe the SCENE ONLY — no people — and always append a
    cinematic style suffix. The avatar stands in the foreground, so we keep it clear.

    When the agent gave a background description we lead with it; otherwise we fall back to
    a generic premium setting tied to the project's location.
    """
    user_prompt = " ".join((user_prompt or "").split())  # also normalises NBSP from data
    bits: list[str] = []
    if user_prompt:
        bits.append(user_prompt)
    else:
        # .split() on each part normalises NBSP (a known district data gotcha) and drops Nones
        loc = " ".join((project.district or "").split() + (project.city or "").split())
        where = f"in {loc}" if loc else "in Dubai"
        bits.append(
            f"A modern, premium real-estate setting {where} suiting the {project.name} "
            "development — contemporary architecture, manicured landscaping, elegant amenity spaces"
        )
    bits.append(
        "photorealistic, cinematic, shallow depth of field, golden-hour lighting, "
        "vertical 9:16 composition, no people, clear foreground for a presenter"
    )
    return ", ".join(bits)[:1400]  # Stability core prompt budget


def _agent_full_name(profile) -> str:
    return " ".join(p for p in (profile.first_name, profile.last_name) if p).strip()


async def _resolve_avatar_voice_by_name(name: str) -> tuple[Optional[dict], Optional[dict], str, list[str], list[str]]:
    """Look up HeyGen avatar + voice by name. Tries the full string first, then the first
    word (so "Zain Ul Abdeen" falls back to "Zain"). Returns (avatar, voice, name_used,
    available_avatars, available_voices). When anything is missing, the available_* lists
    are populated so the caller can show a useful error."""
    name = (name or "").strip()
    if not name:
        return None, None, "", [], []
    candidates = [name]
    parts = name.split()
    if len(parts) > 1 and parts[0] != name:
        candidates.append(parts[0])

    avatar: Optional[dict] = None
    voice: Optional[dict] = None
    name_used = name
    for cand in candidates:
        if avatar is None:
            avatar = await heygen.find_avatar_by_name(cand)
            if avatar:
                name_used = cand
        if voice is None:
            voice = await heygen.find_voice_by_name(cand)
        if avatar and voice:
            break

    available_avatars: list[str] = []
    available_voices: list[str] = []
    if avatar is None:
        available_avatars = [a.get("avatar_name", "") for a in (await heygen.list_avatars())][:30]
    if voice is None:
        available_voices = [v.get("name", "") for v in (await heygen.list_voices())][:30]
    return avatar, voice, name_used, available_avatars, available_voices


async def create_promo_video_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. This feature is only for our agents."}

    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    # Whose HeyGen avatar/voice should the video use?
    # Default: the signed-in agent's own name. If agent_name arg is given, dispatch on
    # behalf of that name — useful for one agent (e.g. Zain) generating videos for the
    # whole team.
    requested_name = (args.get("agent_name") or "").strip()
    target_name = requested_name or _agent_full_name(profile) or (profile.first_name or "")
    if not target_name:
        return {"error": "No name to resolve avatar/voice. Provide agent_name or set first/last name on your profile."}

    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id is required"}
    project = (await db.execute(
        select(Project).where(Project.id == int(project_id))
    )).scalar_one_or_none()
    if project is None:
        return {"error": f"No project found with id {project_id}"}

    try:
        avatar, voice, name_used, avail_avatars, avail_voices = await _resolve_avatar_voice_by_name(target_name)
    except heygen.HeyGenError as e:
        log.error("HeyGen lookup failed: %s", e)
        return {"error": f"Couldn't reach HeyGen to look up avatar/voice: {e}"}

    missing: list[str] = []
    if avatar is None:
        missing.append(f"avatar named '{target_name}'. Please create it in HeyGen → Avatars first.")
    if voice is None:
        missing.append(f"voice named '{target_name}'. Please create/train it in HeyGen → Voices first.")
    if missing:
        msg = f"Missing in HeyGen for {target_name!r}: " + "; ".join(missing)
        if avatar is None and avail_avatars:
            msg += f"\nAvatars currently in the account: {', '.join(a for a in avail_avatars if a) or '(none named)'}"
        if voice is None and avail_voices:
            msg += f"\nVoices currently in the account: {', '.join(v for v in avail_voices if v) or '(none named)'}"
        return {"error": msg}

    explicit_script = (args.get("script") or "").strip()
    background_prompt = (args.get("background_prompt") or "").strip()

    if not explicit_script:
        explicit_script = await _write_campaign_script(project)
        if not explicit_script:
            return {"error": "Failed to write a campaign script. Try again or provide one."}

    # Build the background plate HeyGen composites the avatar in front of: ALWAYS an
    # AI-generated 9:16 image (purpose-built — no people, clear foreground), uploaded to
    # HeyGen's own asset store so HeyGen can always fetch it. We deliberately do NOT fall
    # back to project.cover_image_url — that's a Reelly-origin URL (forbidden as a fetch
    # source, and prone to 403/expiry on HeyGen's server-side fetch). On any failure we
    # drop the override and keep the avatar's default scene rather than fail the whole job.
    # This is the REAL background swap; the old Video-Agent path took the scene as free
    # text and silently ignored it, which is why the background never changed.
    background_url: Optional[str] = None
    background_source = "avatar default scene"
    scene_prompt = _compose_background_prompt(project, background_prompt)
    try:
        png = await bedrock_images.generate_background_png(scene_prompt, aspect_ratio="9:16")
        background_url = await heygen.upload_asset(png, content_type="image/png")
        background_source = (
            f"AI image: {background_prompt[:80]}" if background_prompt
            else "AI image (generic project scene)"
        )
    except (bedrock_images.ImageGenError, heygen.HeyGenError) as e:
        log.warning("background gen/upload failed (%s); using avatar default scene", e)
        background_source = "avatar default scene (bg gen failed)"

    try:
        heygen_video_id = await heygen.generate_video(
            script=explicit_script,
            avatar_id=avatar["avatar_id"],
            voice_id=voice["voice_id"],
            background_url=background_url,
            resolution="1080p",
            aspect_ratio="9:16",
            # Emit a CLEAN video — our Remotion post-step (app/captioning) burns the
            # captions with controlled, title-safe placement. Letting HeyGen also burn
            # its own (un-positionable) captions would double them on every success and
            # is exactly the off-screen-text problem we're fixing.
            caption=False,
        )
    except heygen.HeyGenError as e:
        log.error("HeyGen video generation failed: %s", e)
        return {"error": f"Video service error: {e}"}

    if not heygen_video_id:
        return {"error": "HeyGen did not return a video_id."}

    now = datetime.now(timezone.utc)
    stored_script = explicit_script
    tg_chat_id = ctx.get("telegram_chat_id")
    result = await db.execute(
        insert(Video).values(
            requested_by=user_id,
            project_id=project.id,
            script=stored_script,
            status="processing",
            heygen_video_id=heygen_video_id,
            created_at=now,
            updated_at=now,
            telegram_chat_id=tg_chat_id,
        ).returning(Video.id)
    )
    video_id = result.scalar_one()
    await db.commit()

    log.info(
        "video job started id=%s heygen=%s project=%s by=%s for=%s avatar=%s voice=%s bg=%s tg=%s",
        video_id, heygen_video_id, project.id, user_id, name_used,
        avatar.get("avatar_id"), voice.get("voice_id"), background_source, tg_chat_id,
    )
    return {
        "video_id": str(video_id),
        "status": "processing",
        "project_id": project.id,
        "project_name": project.name,
        "for_agent": name_used,
        "script_preview": (explicit_script or "")[:260],
        "script_word_count": len(explicit_script.split()) if explicit_script else 0,
        "avatar_name": avatar.get("avatar_name"),
        "voice_name": voice.get("name"),
        "background": background_source,
        "format": "1080x1920 vertical (Reels/TikTok)",
        "message": (
            "Video generation started. After HeyGen renders, we burn on Hormozi-style "
            "captions, so total wait is typically 2–3 minutes. "
            + ("You'll get a Telegram message with the captioned video + download link the moment it's ready."
               if tg_chat_id else "Ask \"is my video ready?\" to check.")
        ),
    }


registry.register(Tool(
    name="create_promo_video",
    description=(
        "Generate a short AI-avatar promotional video about a project via HeyGen. "
        "Only available to agents (logged-in profiles with role=salesagent or admin "
        "AND ask_alpha_access in (read, write)). The tool returns a video_id and "
        "starts the job asynchronously — HeyGen renders it and then Hormozi-style captions "
        "are burned on, so it typically takes 2–3 minutes to be ready. "
        "Use this when an agent asks to create a marketing video, promo video, or AI "
        "video about a specific project. If the user only describes the project by name, "
        "first call search_projects to get the numeric project_id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "integer",
                "description": "Numeric project ID to promote.",
            },
            "agent_name": {
                "type": "string",
                "description": (
                    "Optional: the agent the video is being made FOR — name must match a HeyGen "
                    "avatar AND voice in the account (e.g. 'Zain', 'Rami', 'Zain Ul Abdeen'). "
                    "If omitted, the requester's own profile name is used. Use this when the "
                    "logged-in user is generating videos on behalf of teammates."
                ),
            },
            "script": {
                "type": "string",
                "description": "Optional: exact narration script. If omitted, one is written in our house Hook/Value/CTA style from the project's data.",
            },
            "background_prompt": {
                "type": "string",
                "description": (
                    "Optional natural-language description of the background scene to AI-generate "
                    "behind the avatar — e.g. 'Burj Khalifa visible through a floor-to-ceiling glass "
                    "window with the Dubai skyline at golden hour', 'sleek modern marble lobby with "
                    "indoor plants', 'Palm Jumeirah aerial view at dusk'. The server generates a vertical "
                    "9:16 image via Bedrock and uses it as the avatar's backdrop. If omitted, a generic "
                    "project-tailored scene is generated; if image generation fails, the avatar's "
                    "default scene is kept."
                ),
            },
        },
        "required": ["project_id"],
    },
    handler=create_promo_video_handler,
))


async def check_my_video_status_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    video_id_arg = args.get("video_id")
    if video_id_arg:
        try:
            target_id = UUID(str(video_id_arg))
        except (ValueError, TypeError):
            return {"error": f"Invalid video_id: {video_id_arg}"}
        v = (await db.execute(
            select(Video).where(Video.id == target_id, Video.requested_by == user_id)
        )).scalar_one_or_none()
    else:
        v = (await db.execute(
            select(Video).where(Video.requested_by == user_id)
            .order_by(Video.created_at.desc()).limit(1)
        )).scalar_one_or_none()

    if v is None:
        return {"error": "No videos found for you yet. Generate one first."}

    project_name = None
    if v.project_id is not None:
        p = (await db.execute(select(Project.name).where(Project.id == v.project_id))).scalar_one_or_none()
        project_name = p
    # Prefer the captioned cut once it's ready; fall back to the raw HeyGen video.
    return {
        "video_id": str(v.id),
        "status": v.status,
        "caption_status": v.caption_status,
        "video_url": v.captioned_video_url or v.video_url,
        "captioned_video_url": v.captioned_video_url,
        "thumbnail_url": v.thumbnail_url,
        "project_id": v.project_id,
        "project_name": project_name,
        "error_detail": v.error,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "completed_at": v.completed_at.isoformat() if v.completed_at else None,
    }


registry.register(Tool(
    name="check_my_video_status",
    description=(
        "Check the status of a previously-requested promo video. Call this when the agent "
        "asks 'is my video ready?', 'where is my video?', 'send me the link', or any similar "
        "follow-up. Returns status (pending/processing/completed/failed) and, when completed, "
        "a video_url that the agent can share or download. If video_id is not provided, "
        "returns the agent's most recent video. Restricted to agents."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "video_id": {
                "type": "string",
                "description": "Optional UUID of a specific video. Omit to get the latest one.",
            },
        },
        "required": [],
    },
    handler=check_my_video_status_handler,
))
