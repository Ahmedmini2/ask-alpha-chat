import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
import boto3
from sqlalchemy import select, insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.core.profiles import get_profile, is_agent
from app.db.models import Project, Video
from app.integrations import heygen
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.videos")

_bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region)


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


async def _write_campaign_script(project: Project) -> str:
    brief = _campaign_brief(project)

    def _call() -> str:
        resp = _bedrock.converse(
            modelId=settings.bedrock_model_id,
            system=[{"text": CAMPAIGN_SCRIPT_SYSTEM_PROMPT}],
            messages=[{
                "role": "user",
                "content": [{"text":
                    "Write the spoken script from this brief. Only use facts present below; "
                    "omit anything missing. Output the narration as one continuous paragraph "
                    "with no labels.\n\n" + brief
                }],
            }],
            inferenceConfig={"maxTokens": 600, "temperature": 0.6},
        )
        for block in resp["output"]["message"]["content"]:
            if "text" in block:
                return block["text"].strip()
        return ""

    return await asyncio.to_thread(_call)


def _project_facts(project: Project) -> str:
    facts: list[str] = [f"- Name: {project.name}"]
    if project.developer and project.developer.name:
        facts.append(f"- Developer: {project.developer.name}")
    loc = " ".join(p for p in (project.city, project.region, project.country) if p).strip()
    if loc:
        facts.append(f"- Location: {loc}")
    if project.short_description:
        facts.append(f"- Tagline: {project.short_description}")
    if project.description:
        facts.append(f"- About: {project.description[:600]}")
    if project.min_price and project.max_price and project.currency:
        facts.append(f"- Price range: {project.currency} {project.min_price:,.0f}–{project.max_price:,.0f}")
    if project.completion_quarter:
        facts.append(f"- Completion: {project.completion_quarter}")
    if project.sale_status:
        facts.append(f"- Sale status: {project.sale_status}")
    return "\n".join(facts)


def _build_agent_prompt(project: Project, explicit_script: str, background_prompt: str) -> str:
    """Compose the natural-language prompt sent to HeyGen's Video Agent (/v3/video-agents).
    HeyGen handles script writing (if no explicit_script) AND scene/background generation."""
    parts: list[str] = []

    parts.append(
        "Create a short vertical (9:16) real-estate promo video, native for "
        "Instagram Reels / TikTok. The avatar should present naturally and "
        "professionally, with confident posture and natural delivery."
    )
    parts.append("\nPROJECT DETAILS:\n" + _project_facts(project))

    if explicit_script:
        parts.append(
            "\nSCRIPT (read verbatim, do not change wording):\n" + explicit_script
        )
    else:
        parts.append(
            "\nSCRIPT GUIDELINES:\n"
            "- Write a 60–90 word first-person narration.\n"
            "- Conversational, professional, calm. No slogans, no markdown.\n"
            "- Mention project name, developer, city, and 2–3 standout details.\n"
            "- At most one price or timeline figure.\n"
            "- End with a soft call to action like \"Reach out for full details.\""
        )

    if background_prompt:
        parts.append(
            "\nSCENE / BACKGROUND:\n"
            f"{background_prompt}\n"
            "Photorealistic, cinematic, depth of field. The avatar should be "
            "naturally composited in the foreground of this scene."
        )
    else:
        parts.append(
            "\nSCENE / BACKGROUND:\n"
            "A modern, premium Dubai real-estate setting that fits the project — "
            "an architectural showroom, sleek office, or scenic skyline view. "
            "Photorealistic, cinematic, depth of field."
        )

    parts.append("\nDELIVERY:\n- Vertical 9:16. Burn-in subtitles. Resolution 1080p.")

    return "\n".join(parts)


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

    agent_prompt = _build_agent_prompt(project, explicit_script, background_prompt)

    try:
        session_id, heygen_video_id = await heygen.generate_video_via_agent(
            prompt=agent_prompt,
            avatar_id=avatar["avatar_id"],
            voice_id=voice["voice_id"],
            orientation="portrait",
        )
    except heygen.HeyGenError as e:
        log.error("HeyGen video-agent failed: %s", e)
        return {"error": f"Video service error: {e}"}

    if not heygen_video_id and session_id:
        # Some sessions assign video_id a beat later; poll briefly.
        for _ in range(6):
            await asyncio.sleep(2)
            try:
                sess = await heygen.get_agent_session(session_id)
            except heygen.HeyGenError:
                continue
            heygen_video_id = sess.get("video_id")
            if heygen_video_id:
                break
    if not heygen_video_id:
        return {"error": "HeyGen did not return a video_id (session_id: %s)." % session_id}

    script_for_log = explicit_script or "(written by HeyGen agent from prompt)"
    background_source = (
        f"agent-generated scene: {background_prompt[:80]}"
        if background_prompt else "agent-generated scene"
    )

    now = datetime.now(timezone.utc)
    stored_script = explicit_script or agent_prompt[:4000]
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
        "video job started id=%s heygen=%s session=%s project=%s by=%s for=%s avatar=%s voice=%s bg=%s tg=%s",
        video_id, heygen_video_id, session_id, project.id, user_id, name_used,
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
            "Video generation started. Typical wait is 1–2 minutes. "
            + ("You'll get a Telegram message with the download link the moment it's ready."
               if tg_chat_id else "Ask \"is my video ready?\" to check.")
        ),
    }


registry.register(Tool(
    name="create_promo_video",
    description=(
        "Generate a short AI-avatar promotional video about a project via HeyGen. "
        "Only available to agents (logged-in profiles with role=salesagent or admin "
        "AND ask_alpha_access in (read, write)). The tool returns a video_id and "
        "starts the job asynchronously — it typically takes 1–2 minutes to render. "
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
                    "9:16 image via Bedrock and uses it as the avatar's backdrop. If omitted, the "
                    "project's cover photo is used."
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
    return {
        "video_id": str(v.id),
        "status": v.status,
        "video_url": v.video_url,
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
