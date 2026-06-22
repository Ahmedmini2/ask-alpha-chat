import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
import boto3
import httpx
from sqlalchemy import select, insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.core.profiles import (
    get_profile, is_agent, get_heygen_avatar, heygen_avatar_status_error,
)
from app.db.models import AskAlphaMessage, Project, Video
from app.integrations import bedrock_images, heygen
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

3. CTA (1–2 sentences, ~25–45 words). State the starting price and the payment plan (e.g., "60/40 \
plan"). Frame the price as "the best entry point" rather than "affordable". End with the action: \
"Submit your EOI today to get priority access."

VOICE & RHYTHM:
- Spoken, NOT read. Contractions mandatory ("it's", "we're", "you're").
- Numbers must be speakable: "38 million square feet", "1.35 million dirhams" — never "38,000,000".
- CURRENCY: always say prices in spoken "dirhams" AFTER the amount ("1.35 million dirhams"). NEVER \
write the code "AED" — the avatar voice cannot pronounce it. Never use "$" or "USD".
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


async def _write_campaign_script(project: Project) -> str:
    """Write the house-style narration with our main Claude model on Bedrock
    (settings.bedrock_model_id — the same model the rest of the app uses)."""
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


# Distinct lead angles so the three drafted variations actually differ.
_SCRIPT_ANGLES = (
    "Angle for THIS version: lead with the lifestyle and the standout amenities.",
    "Angle for THIS version: lead with the investment case — entry price, payment plan, value.",
    "Angle for THIS version: lead with the location and the nearby landmarks.",
)


async def _write_campaign_scripts(project: Project) -> list[str]:
    """Draft up to three DISTINCT script variations (one per lead angle) for the agent to choose
    from. Each uses the same house CAMPAIGN_SCRIPT_SYSTEM_PROMPT; only the opening angle differs.
    Runs the calls concurrently."""
    brief = _campaign_brief(project)

    def _call(angle: str) -> str:
        resp = _bedrock.converse(
            modelId=settings.bedrock_model_id,
            system=[{"text": CAMPAIGN_SCRIPT_SYSTEM_PROMPT}],
            messages=[{
                "role": "user",
                "content": [{"text":
                    "Write the spoken script from this brief. Only use facts present below; "
                    "omit anything missing. Output the narration as one continuous paragraph "
                    "with no labels.\n\n" + angle + "\n\n" + brief
                }],
            }],
            inferenceConfig={"maxTokens": 600, "temperature": 0.8},
        )
        for block in resp["output"]["message"]["content"]:
            if "text" in block:
                return block["text"].strip()
        return ""

    results = await asyncio.gather(
        *[asyncio.to_thread(_call, a) for a in _SCRIPT_ANGLES], return_exceptions=True
    )
    scripts: list[str] = []
    for r in results:
        if isinstance(r, str) and r and r not in scripts:  # drop blanks + exact dupes
            scripts.append(r)
    return scripts


def _compose_background_prompt(project: Project, user_prompt: str, aspect_ratio: str = "9:16") -> str:
    """Build the text-to-image prompt for the background plate that HeyGen composites
    the avatar in front of. Describe the SCENE ONLY — no people — and always append a
    cinematic style suffix. The avatar stands in the foreground, so we keep it clear.

    The composition note matches the video's orientation (portrait 9:16 vs landscape 16:9)
    so the generated plate frames correctly behind the avatar.

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
    composition = ("horizontal 16:9 composition" if aspect_ratio == "16:9"
                   else "vertical 9:16 composition")
    bits.append(
        "photorealistic, cinematic, shallow depth of field, golden-hour lighting, "
        f"{composition}, no people, clear foreground for a presenter"
    )
    return ", ".join(bits)[:1400]  # Stability core prompt budget


# Magnitude abbreviations a TTS voice mangles ("1.4M" -> "one point four em"); we spell them.
_MAGNITUDE = {"m": "million", "k": "thousand", "b": "billion", "bn": "billion"}
_ABBR_RE = re.compile(r"(\d(?:[\d,.]*\d)?)\s*(bn|[mkb])\b", re.IGNORECASE)
_AED_BEFORE_RE = re.compile(
    r"\bAED\s*((?:\d[\d,.]*)(?:\s*(?:million|billion|thousand))?)", re.IGNORECASE)
_AED_AFTER_RE = re.compile(
    r"((?:\d[\d,.]*)(?:\s*(?:million|billion|thousand))?)\s*AED\b", re.IGNORECASE)


def _to_spoken_money(script: str) -> str:
    """Make currency amounts SPEAKABLE for the avatar voice. The TTS can't pronounce the
    'AED' code (it spells it out or stumbles), so every AED becomes the spoken word
    'dirhams', placed AFTER the amount the way a person says it ('AED 1.4 million' -> '1.4
    million dirhams'). Magnitude abbreviations (1.4M, 850K) are spelled out first so they're
    speakable too. Idempotent and safe to run on any final script."""
    if not script:
        return script
    s = _ABBR_RE.sub(lambda m: f"{m.group(1)} {_MAGNITUDE[m.group(2).lower()]}", script)
    s = _AED_BEFORE_RE.sub(lambda m: f"{m.group(1).strip()} dirhams", s)
    s = _AED_AFTER_RE.sub(lambda m: f"{m.group(1).strip()} dirhams", s)
    s = re.sub(r"\bAED\b", "dirhams", s, flags=re.IGNORECASE)  # any standalone leftover
    return re.sub(r"\s{2,}", " ", s).strip()


async def _look_aspect_ratio(look: dict) -> tuple[str, str]:
    """Match the video's orientation to the chosen look. HeyGen looks carry no orientation
    field, so we read the look's preview image: wider-than-tall -> landscape 16:9, else
    portrait 9:16. Falls back to portrait (our default) when the image can't be read.
    Returns (aspect_ratio, orientation_label)."""
    url = (look or {}).get("preview_url")
    if url:
        data = await _fetch_bytes(url)
        if data:
            try:
                from PIL import Image
                w, h = Image.open(io.BytesIO(data)).size
                if w and h and w > h:
                    return "16:9", "landscape"
                return "9:16", "portrait"
            except Exception as e:  # pragma: no cover — Pillow missing / corrupt image
                log.warning("look orientation detect failed (%s); defaulting portrait", e)
    return "9:16", "portrait"


def _agent_full_name(profile) -> str:
    return " ".join(p for p in (profile.first_name, profile.last_name) if p).strip()


def _norm_name(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _email_local(profile) -> str:
    """The part of the email before '@', with separators turned into spaces — the website names
    avatars after this (e.g. 'ahmed.othman'), so it doubles as a display/voice name when a profile
    has no first/last name set."""
    raw = (getattr(profile, "email", None) or "")
    local = raw.split("@", 1)[0]
    return " ".join(local.replace(".", " ").replace("_", " ").replace("-", " ").split()).strip()


def _self_display_name(profile, av) -> str:
    """A human name for the SIGNED-IN user, used to resolve their voice and label the video.
    Profile full name first, then their connected avatar's name, then the email local part."""
    return (
        _agent_full_name(profile)
        or (av.name.strip() if av and av.name else "")
        or _email_local(profile)
        or (profile.first_name or "")
    ).strip()


def _self_identity_tokens(profile, av) -> set:
    """Every normalized name that legitimately refers to the signed-in user — used to detect when
    a model-supplied `agent_name` is pointing at SOMEONE ELSE."""
    names = {
        _norm_name(_agent_full_name(profile)),
        _norm_name(profile.first_name or ""),
        _norm_name(profile.last_name or ""),
        _norm_name(_email_local(profile)),
    }
    if av and av.name:
        names.add(_norm_name(av.name))
        names.add(_norm_name(av.name.replace(".", " ").replace("_", " ").replace("-", " ")))
    names.discard("")
    return names


def _agent_name_targets_other(requested: str, profile, av) -> bool:
    """True when an explicit `agent_name` clearly refers to a DIFFERENT person than the caller.

    This only powers a clear error message — security does NOT depend on it: the avatar is always
    resolved from the caller's own user_id / own profile name regardless. We accept a name as
    "self" on an exact match or a shared first-name token (so 'Zain' is fine for 'Zain Ul Abdeen'),
    and reject anything with no overlap ('Chinoy', 'Ahmed' for someone else)."""
    r = _norm_name(requested)
    if not r:
        return False  # no name supplied → defaults to self
    mine = _self_identity_tokens(profile, av)
    if not mine:
        # We can't identify the caller by name at all; don't claim it's "someone else".
        return False
    if r in mine:
        return False
    r0 = r.split()[0]
    mine_tokens = {tok for name in mine for tok in name.split()}
    return r0 not in mine_tokens


_AV_UNSET = object()  # sentinel: caller didn't pass a pre-fetched heygen_avatars row


async def _resolve_self_avatar(db: AsyncSession, profile, user_id, av=_AV_UNSET) -> tuple[Optional[list], Optional[dict], dict]:
    """Resolve the avatar LOOKS for the signed-in user — and ONLY them. Never another person's.

    Priority:
      1) the user's connected HeyGen avatar in `heygen_avatars` (authoritative — keyed by user_id).
         When such a row exists it is the single source of truth: if it isn't render-ready we fail
         with a clear reason rather than fall through to name-matching (which could resolve a
         different person's avatar).
      2) legacy users with no connected row: a HeyGen avatar group whose name SAFELY matches the
         user's OWN profile/email name (exact or unambiguous token-subset — never a shared first
         name, so it can't resolve a teammate who happens to share a first name).

    `av` may be a pre-fetched `heygen_avatars` row (the handler already loads one for the guard) to
    avoid a second query; omit it to fetch here. Returns (looks, error_dict, meta) — exactly one of
    looks / error_dict is non-None. `meta` carries `display_name`, `source`, and the `avatar` row."""
    if av is _AV_UNSET:
        av = await get_heygen_avatar(db, user_id)
    display_name = _self_display_name(profile, av)
    meta = {"display_name": display_name, "source": None, "avatar": av}

    if av is not None:
        status_err = heygen_avatar_status_error(av)
        if status_err:
            return None, {"error": status_err}, meta
        try:
            looks = await heygen.looks_for_connected_avatar(
                av.group_id, av.avatar_id, av.preview_image_url, display_name or "your avatar"
            )
        except heygen.HeyGenError as e:
            log.error("HeyGen connected-avatar lookup failed for %s: %s", user_id, e)
            return None, {"error": f"Couldn't reach HeyGen to load your avatar: {e}"}, meta
        if looks:
            meta["source"] = "connected"
            return looks, None, meta
        return None, {"error": "Your connected avatar returned no usable looks from HeyGen. "
                               "Please re-record it in Alpha Chat → Settings."}, meta

    # No connected record — legacy path, matched on the user's OWN name only (SAFELY — exact /
    # unambiguous, never a shared first-name token; see heygen.list_looks_for_self).
    if not display_name:
        return None, {"error": "No AI avatar is connected to your account. Record one in Alpha Chat "
                               "→ Settings to create your avatar, then try again."}, meta
    identity = _self_identity_tokens(profile, av) | {_norm_name(display_name)}
    try:
        looks = await heygen.list_looks_for_self(identity, display_name)
    except heygen.HeyGenError as e:
        log.error("HeyGen name lookup failed for %r: %s", display_name, e)
        return None, {"error": f"Couldn't reach HeyGen to look up your avatar: {e}"}, meta
    if looks:
        meta["source"] = "name-match"
        return looks, None, meta
    return None, {"error": f"No AI avatar found for your account ({display_name!r}). Record one in "
                           "Alpha Chat → Settings to create your avatar."}, meta


async def _resolve_voice(agent_name: str, look: Optional[dict]) -> tuple[Optional[dict], str, list[str]]:
    """Resolve the HeyGen voice for an agent, in priority order:
      1) an explicit per-agent pin in settings.heygen_agent_voices (AUTHORITATIVE — this is
         how you guarantee e.g. "Said" always speaks in Said's cloned voice and never a
         same-named stock preset);
      2) the voice HeyGen attached to the chosen avatar look (best-effort — only when the
         group API actually returns one);
      3) a name search across the account's voices (full name, then first token).

    Returns (voice_or_None, source, available_voice_names). The name list is filled only
    when nothing matched, so the caller can show a useful error.

    The old code did only step 3 against the FULL voice library (presets included), so the
    avatar and the voice were resolved independently and could drift apart — that's the bug
    where Said's avatar spoke in a stranger's voice.
    """
    agent_name = (agent_name or "").strip()
    if not agent_name:
        return None, "none", []
    norm = _norm_name(agent_name)
    first = norm.split()[0] if norm.split() else ""

    # 1) explicit pin
    vmap = settings.agent_voice_map
    pinned = vmap.get(norm) or (vmap.get(first) if first else None)
    if pinned:
        return {"voice_id": pinned, "name": f"{agent_name} (pinned)"}, "config-pin", []

    # 2) the look's HeyGen-attached voice
    look_voice = (look or {}).get("default_voice_id")
    if look_voice:
        return {"voice_id": look_voice, "name": f"{agent_name} (avatar voice)"}, "look-attached", []

    # 3) EXACT full-name search across the account voices. We deliberately do NOT fall back to the
    #    first-name token here: a first-token match ("Ahmed") can bind a same-first-name stranger's
    #    voice to your own avatar — the very "avatar speaks in a stranger's voice" drift this resolver
    #    exists to prevent. Pin a voice (HEYGEN_AGENT_VOICES) or attach one to the avatar look instead.
    v = await heygen.find_voice_by_name(agent_name)
    if v:
        return v, "name-match", []
    return None, "none", [v.get("name", "") for v in (await heygen.list_voices())][:30]


def _to_jpeg(img_bytes: bytes) -> Optional[bytes]:
    """Convert any preview image (HeyGen serves WEBP) to JPEG so Telegram sendPhoto
    accepts it. Returns None if Pillow is unavailable or decoding fails."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes))
        if im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=88)
        return out.getvalue()
    except Exception as e:  # pragma: no cover — Pillow missing / corrupt image
        log.warning("preview JPEG conversion failed: %s", e)
        return None


async def _fetch_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(url)
            if r.status_code < 400:
                return r.content
            log.warning("preview fetch %s -> %s", url[:60], r.status_code)
    except Exception as e:  # pragma: no cover
        log.warning("preview fetch error: %s", e)
    return None


async def _send_telegram_photo(chat_id: int, image_bytes: bytes, caption: str) -> bool:
    """Send one look preview as a photo (name as caption). Converts to JPEG first; if that
    isn't possible, falls back to sendDocument (Telegram renders the image inline)."""
    if not settings.telegram_bot_token:
        return False
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    jpeg = await asyncio.to_thread(_to_jpeg, image_bytes)
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            if jpeg is not None:
                r = await c.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": str(chat_id), "caption": caption[:1024]},
                    files={"photo": ("look.jpg", jpeg, "image/jpeg")},
                )
            else:
                r = await c.post(
                    f"{base}/sendDocument",
                    data={"chat_id": str(chat_id), "caption": caption[:1024]},
                    files={"document": ("look.webp", image_bytes, "image/webp")},
                )
            if r.status_code >= 400:
                log.warning("telegram sendPhoto failed %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        log.warning("telegram sendPhoto error: %s", e)
        return False


async def _resolve_project(db: AsyncSession, args: dict) -> tuple[Optional[Project], Optional[dict]]:
    """Resolve the target project. `project_name` is AUTHORITATIVE — it's the literal name the
    agent picked, so (unlike a numeric id the model has to recall across turns) it can't drift
    to a different project. Exact case-insensitive name match wins; a unique containment match
    is accepted; anything ambiguous or missing returns an error rather than guessing — that's
    what stops "Farm Gardens Villas" from rendering as "Verdana 4". Falls back to project_id
    only when no name is given. Returns (project, error_dict); exactly one is non-None."""
    name = (args.get("project_name") or "").strip()
    project_id = args.get("project_id")

    # The UI lists results as "Name (Developer, City)" — if the model passes that label
    # verbatim, drop the trailing parenthetical so the bare name still matches.
    if name.endswith(")") and "(" in name:
        name = name[:name.rfind("(")].strip()

    if name:
        exact = (await db.execute(select(Project).where(Project.name.ilike(name)))).scalars().all()
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, {"error": f"Several projects are named {name!r}: "
                          + ", ".join(f"{p.name} (id {p.id})" for p in exact[:6])
                          + ". Ask which one, then pass its exact name or project_id."}
        like = (await db.execute(
            select(Project).where(Project.name.ilike(f"%{name}%")).limit(6)
        )).scalars().all()
        if len(like) == 1:
            return like[0], None
        if len(like) > 1:
            return None, {"error": f"{name!r} matches several projects: "
                          + ", ".join(p.name for p in like)
                          + ". Ask the agent which one and pass that exact name."}
        return None, {"error": f"No project named {name!r} in our system. Search first, then "
                               "pass the exact name the agent picks."}

    if project_id:
        p = (await db.execute(
            select(Project).where(Project.id == int(project_id))
        )).scalar_one_or_none()
        if p is None:
            return None, {"error": f"No project found with id {project_id}"}
        return p, None

    return None, {"error": "Provide project_name (preferred — the exact name the agent picked) or project_id."}


async def create_promo_video_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. This feature is only for our agents."}

    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    # The video ALWAYS uses the signed-in user's OWN avatar — never anyone else's. A user can
    # only generate as themselves: the avatar is resolved from their user_id (their connected
    # `heygen_avatars` row) or, for legacy users, a group bearing their own profile name. We load
    # the connected row ONCE and reuse it for both the cross-person guard and the resolver.
    av = await get_heygen_avatar(db, user_id)
    requested_name = (args.get("agent_name") or "").strip()
    if _agent_name_targets_other(requested_name, profile, av):
        return {"error": f"You can only generate a promo video with your OWN AI avatar, not "
                         f"{requested_name!r}'s. Each agent's avatar is tied to their account — "
                         "ask them to generate their own video."}

    project, project_err = await _resolve_project(db, args)
    if project_err:
        return project_err

    looks, looks_err, meta = await _resolve_self_avatar(db, profile, user_id, av=av)
    if looks_err:
        return looks_err
    target_name = meta["display_name"]

    # Which look? Explicit choice wins; a single-look avatar needs no choice; otherwise the
    # agent must pick first (this enforces the look question even if the model skipped it).
    look_arg = (args.get("look") or "").strip()
    if look_arg:
        chosen = heygen.find_look_in(looks, look_arg)
        if chosen is None:
            names = [lk["look_name"] for lk in looks]
            return {"error": f"Couldn't match look {look_arg!r} for your avatar. "
                             f"Available looks: {', '.join(names)}"}
    elif len(looks) == 1:
        chosen = looks[0]
    else:
        return {
            "error": "Multiple looks available — ask the agent which look to use before generating.",
            "needs_look_choice": True,
            "agent_name": target_name,
            "looks": [lk["look_name"] for lk in looks],
        }

    name_used = target_name

    # Resolve the VOICE *after* the look so we can prefer the voice HeyGen attached to it.
    # This is what keeps the avatar and voice from drifting apart (the bug where Said's
    # avatar spoke in a stranger's voice).
    try:
        voice, voice_source, avail_voices = await _resolve_voice(target_name, chosen)
    except heygen.HeyGenError as e:
        log.error("HeyGen voice lookup failed: %s", e)
        return {"error": f"Couldn't reach HeyGen to look up the voice: {e}"}
    if voice is None:
        msg = (f"Missing in HeyGen: no voice for {target_name!r}. Train/name a voice for them in "
               f"HeyGen → Voices, or pin one with HEYGEN_AGENT_VOICES (agent name → voice_id).")
        if avail_voices:
            msg += f"\nVoices currently in the account: {', '.join(v for v in avail_voices if v) or '(none named)'}"
        return {"error": msg}

    explicit_script = (args.get("script") or "").strip()
    background_prompt = (args.get("background_prompt") or "").strip()

    if not explicit_script:
        explicit_script = await _write_campaign_script(project)
        if not explicit_script:
            return {"error": "Failed to write a campaign script. Try again or provide one."}

    # The avatar voice can't pronounce "AED" — convert every amount to spoken "dirhams"
    # (and spell magnitude abbreviations) on the FINAL script, whoever wrote it.
    explicit_script = _to_spoken_money(explicit_script)

    # Match the video orientation to the chosen look (portrait vs landscape), and build the
    # background plate at the SAME aspect so it frames correctly behind the avatar.
    aspect_ratio, orientation = await _look_aspect_ratio(chosen)

    # Build the background plate HeyGen composites the avatar in front of: ALWAYS an
    # AI-generated image (purpose-built — no people, clear foreground), uploaded to HeyGen's
    # own asset store so HeyGen can always fetch it. We deliberately do NOT fall back to
    # project.cover_image_url — that's a Reelly-origin URL (forbidden as a fetch source, and
    # prone to 403/expiry on HeyGen's server-side fetch). On any failure we drop the override
    # and keep the avatar's default scene rather than fail the whole job. This is the REAL
    # background swap; the old Video-Agent path took the scene as free text and silently
    # ignored it, which is why the background never changed.
    background_url: Optional[str] = None
    background_source = "avatar default scene"
    scene_prompt = _compose_background_prompt(project, background_prompt, aspect_ratio)
    try:
        png = await bedrock_images.generate_background_png(scene_prompt, aspect_ratio=aspect_ratio)
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
            avatar_id=chosen["avatar_id"],
            voice_id=voice["voice_id"],
            background_url=background_url,
            resolution="1080p",
            aspect_ratio=aspect_ratio,
            # Emit a CLEAN video (no HeyGen burn-in). Captions are added afterward by the
            # Descript post-step in the poller (when DESCRIPT_API_TOKEN is set); HeyGen's own
            # captions are un-positionable and ran off-screen, so they stay off here. If you
            # ever drop Descript, flip to caption=True for HeyGen's built-in captions.
            caption=False,
            is_photo=chosen["is_photo"],
        )
    except heygen.HeyGenError as e:
        log.error("HeyGen video generation failed: %s", e)
        return {"error": f"Video service error: {e}"}

    if not heygen_video_id:
        return {"error": "HeyGen did not return a video_id."}

    now = datetime.now(timezone.utc)
    stored_script = explicit_script
    tg_chat_id = ctx.get("telegram_chat_id")
    # Allegiance outro opt-in (asked after the script is confirmed). The poller appends the
    # orientation-correct outro with a short crossfade as the final post-edit step when true.
    add_outro = bool(args.get("add_outro"))
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
            add_outro=add_outro,
        ).returning(Video.id)
    )
    # How the finished link reaches the agent depends on the channel: Telegram gets an
    # automatic push from the poller; the web app has NO Telegram, so the agent polls
    # ("is my video ready?"). Tell them the truth for THEIR channel — never promise
    # Telegram on web.
    on_telegram = bool(tg_chat_id) or (ctx.get("channel") or "").lower() == "telegram"
    delivery = (
        "You'll get a Telegram message with the captioned download link the moment it's ready."
        if on_telegram else
        "It'll be ready here in a couple of minutes — just ask \"is my video ready?\" and I'll "
        "post the download link right here in this chat."
    )
    video_id = result.scalar_one()
    await db.commit()

    log.info(
        "video job started id=%s heygen=%s project=%s by=%s for=%s look=%r avatar=%s photo=%s voice=%s(%s) bg=%s aspect=%s tg=%s",
        video_id, heygen_video_id, project.id, user_id, name_used, chosen["look_name"],
        chosen["avatar_id"], chosen["is_photo"], voice.get("voice_id"), voice_source, background_source, aspect_ratio, tg_chat_id,
    )
    return {
        "video_id": str(video_id),
        "status": "processing",
        "project_id": project.id,
        "project_name": project.name,
        "for_agent": name_used,
        "look": chosen["look_name"],
        "script_preview": (explicit_script or "")[:260],
        "script_word_count": len(explicit_script.split()) if explicit_script else 0,
        "avatar_name": name_used,
        "voice_name": voice.get("name"),
        "background": background_source,
        "orientation": orientation,
        "aspect_ratio": aspect_ratio,
        "format": ("1920x1080 landscape (16:9)" if aspect_ratio == "16:9"
                   else "1080x1920 portrait (9:16, Reels/TikTok)"),
        "add_outro": add_outro,
        "delivery_channel": "telegram" if on_telegram else "web",
        "message": "Video generation started. Typical wait is 2–4 minutes. " + delivery,
    }


registry.register(Tool(
    name="create_promo_video",
    description=(
        "Generate a short AI-avatar promotional video about a project via HeyGen. "
        "Only available to agents (logged-in profiles with role=salesagent or admin "
        "AND ask_alpha_access in (read, write)). The tool returns a video_id and "
        "starts the job asynchronously — it typically takes 1–2 minutes to render. "
        "The video ALWAYS uses the SIGNED-IN agent's OWN avatar and voice — there is no way to "
        "generate a video as another person, so do NOT ask who it's for and never pass a name. "
        "IMPORTANT: do NOT call this until the agent has chosen an avatar look — first call "
        "list_avatar_looks and ask them which look to use, then pass it as `look`. (If the "
        "avatar has only one look, list_avatar_looks returns single_look and you may call this "
        "directly.) Identify the project by NAME via project_name — pass the EXACT name the agent "
        "picked from the search_projects results (e.g. 'Farm Gardens Villas'); the server resolves "
        "it. Do NOT re-search with the developer/city appended and do NOT guess a numeric id — that "
        "is how the wrong project gets promoted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": (
                    "Preferred. The EXACT project name the agent chose from search results, e.g. "
                    "'Farm Gardens Villas'. The server resolves it (exact name match). Use this "
                    "rather than a numeric id you'd have to recall across turns."
                ),
            },
            "project_id": {
                "type": "integer",
                "description": "Numeric project ID — only if you already have it from search. Never guess it.",
            },
            "look": {
                "type": "string",
                "description": (
                    "The avatar look the agent chose, by name (e.g. 'Dubai Executive', 'The Golf "
                    "Concierge') — must be one of the names list_avatar_looks returned. Omit ONLY "
                    "when the avatar has a single look. If it doesn't match, the tool replies with "
                    "the available look names so you can re-ask."
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
            "add_outro": {
                "type": "boolean",
                "description": (
                    "Whether to append the Allegiance branded outro to the end of the video. Set "
                    "from the agent's yes/no answer to the outro question asked AFTER the script is "
                    "confirmed (STEP 5 of the promo-video flow). true = add it, false/omitted = don't. "
                    "The server picks the portrait or landscape outro automatically to match the video."
                ),
            },
        },
        "required": [],
    },
    handler=create_promo_video_handler,
))


# Cap on project photos attached as cinematic references (HeyGen allows ≤9 images across avatars +
# references; the user asked for up to 4 project photos).
MAX_CINEMATIC_REFERENCES = 4


async def _project_reference_urls(db: AsyncSession, project: Project, k: int = MAX_CINEMATIC_REFERENCES) -> list[str]:
    """Up to `k` of the project's own photos as HeyGen-hosted reference URLs for the cinematic
    `references`. Project assets are often WEBP (or other formats) that the Seedance pipeline
    rejects, so each image is downloaded from our private S3, re-encoded to JPEG, and uploaded to
    HeyGen's asset store — which returns a URL HeyGen will always accept and can always fetch.
    Best-effort: returns 0..k URLs, never raises, and silently drops any image that can't be
    fetched, converted, or uploaded (so one bad asset can't sink the whole job)."""
    from app.brochures import data as brochure_data, storage  # lazy: avoid import cycles
    try:
        images, _plans = await brochure_data._gather_assets(db, project)
    except Exception as e:
        log.warning("cinematic: project asset gather failed: %s", e)
        return []
    urls: list[str] = []
    for a in images:
        if len(urls) >= k:
            break
        raw = await storage.fetch_asset_bytes(a.s3_bucket, a.s3_key)
        if not raw:
            continue
        jpeg = await asyncio.to_thread(_to_jpeg, raw)  # WEBP/PNG/etc → JPEG (Seedance-safe)
        if not jpeg:
            continue
        try:
            urls.append(await heygen.upload_asset(jpeg, content_type="image/jpeg"))
        except heygen.HeyGenError as e:
            log.warning("cinematic: reference upload failed (%s); skipping image", e)
    return urls


def _compose_cinematic_prompt(scene_prompt: str, spoken_line: str, project: Project) -> str:
    """Build the natural-language prompt Seedance renders: the SCENE plus the avatar speaking the
    agreed line. The avatar's likeness comes from the avatar_id (not named here); we describe a
    generic 'presenter' in the scene. Falls back to a project-tailored scene when none is given."""
    scene = " ".join((scene_prompt or "").split())
    if not scene:
        loc = " ".join((project.district or "").split() + (project.city or "").split()) or "Dubai"
        scene = (f"A real-estate presenter in a modern, premium setting for the {project.name} "
                 f"development in {loc} — contemporary architecture, elegant interiors, natural light")
    line = " ".join((spoken_line or "").split())
    return (
        f"{scene}. The presenter looks directly at the camera and says: \"{line}\". "
        "Photorealistic, cinematic, smooth camera motion, shallow depth of field, natural lighting."
    )[:10000]


# A HeyGen Cinematic (Seedance) video is a single ~15s clip; the Allegiance outro (~5s) is appended.
CINEMATIC_CLIP_SECONDS = 15


async def create_cinematic_video_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. This feature is only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    # Same hard rule as the scripted promo: the clip ALWAYS uses the signed-in agent's OWN avatar.
    av = await get_heygen_avatar(db, user_id)
    requested_name = (args.get("agent_name") or "").strip()
    if _agent_name_targets_other(requested_name, profile, av):
        return {"error": f"You can only generate a cinematic video with your OWN AI avatar, not "
                         f"{requested_name!r}'s. Each agent's avatar is tied to their account."}

    project, project_err = await _resolve_project(db, args)
    if project_err:
        return project_err

    looks, looks_err, meta = await _resolve_self_avatar(db, profile, user_id, av=av)
    if looks_err:
        return looks_err
    target_name = meta["display_name"]

    # Cinematic mode NEVER asks which look to use — it always uses the agent's DEFAULT avatar: their
    # connected avatar_id (the twin they recorded) when present, else their primary resolved look.
    # `looks` is guaranteed non-empty here (_resolve_self_avatar returned looks, not an error).
    av_row = meta.get("avatar")
    chosen = None
    if av_row is not None and getattr(av_row, "avatar_id", None):
        chosen = next((lk for lk in looks if lk["avatar_id"] == av_row.avatar_id), None)
    if chosen is None:
        chosen = looks[0]

    spoken_line = " ".join((args.get("spoken_line") or "").split())
    if not spoken_line:
        return {"error": "Provide spoken_line — the short script the avatar should say "
                         "(it's a ~15s clip, so keep it to roughly 30–40 words)."}
    # The avatar voice can't pronounce "AED"; speak amounts as "dirhams" (same rule as scripted).
    spoken_line = _to_spoken_money(spoken_line)
    scene_prompt = (args.get("scene_prompt") or "").strip()

    aspect_ratio = (args.get("aspect_ratio") or "9:16").strip()
    if aspect_ratio not in ("9:16", "16:9", "1:1"):
        aspect_ratio = "9:16"

    # Project photos uploaded to HeyGen once and attached as references.
    reference_urls = await _project_reference_urls(db, project)
    full_prompt = _compose_cinematic_prompt(scene_prompt, spoken_line, project)

    async def _submit(refs: Optional[list]) -> str:
        return await heygen.generate_cinematic_video(
            full_prompt,
            [chosen["avatar_id"]],
            reference_urls=refs,
            aspect_ratio=aspect_ratio,
            resolution="1080p",
            duration=CINEMATIC_CLIP_SECONDS,
            title=f"{project.name} (cinematic)",
        )

    used_refs = bool(reference_urls)
    try:
        heygen_video_id = await _submit(reference_urls)
    except heygen.HeyGenError as e:
        # A bad/unsupported reference image must NEVER block generation — retry with the avatar only
        # (the avatar alone renders fine; we just lose the project photos as scene guides).
        if reference_urls:
            log.warning("cinematic with %d refs failed (%s); retrying avatar-only",
                        len(reference_urls), e)
            try:
                heygen_video_id = await _submit(None)
                used_refs = False
            except heygen.HeyGenError as e2:
                log.error("HeyGen cinematic generation failed (avatar-only): %s", e2)
                return {"error": f"Video service error: {e2}"}
        else:
            log.error("HeyGen cinematic generation failed: %s", e)
            return {"error": f"Video service error: {e}"}
    if not heygen_video_id:
        return {"error": "HeyGen did not return a video_id."}

    now = datetime.now(timezone.utc)
    tg_chat_id = ctx.get("telegram_chat_id")
    # Cinematic always gets the Allegiance outro appended (per product decision); the spoken line is
    # stored as `script` so the poller's caption step has the ground-truth text to burn in.
    result = await db.execute(
        insert(Video).values(
            requested_by=user_id,
            project_id=project.id,
            script=spoken_line,
            status="processing",
            heygen_video_id=heygen_video_id,
            created_at=now,
            updated_at=now,
            telegram_chat_id=tg_chat_id,
            add_outro=True,
            mode="cinematic",
        ).returning(Video.id)
    )
    on_telegram = bool(tg_chat_id) or (ctx.get("channel") or "").lower() == "telegram"
    delivery = (
        "You'll get a Telegram message with the download link the moment it's ready."
        if on_telegram else
        "It'll be ready here in a couple of minutes — just ask \"is my video ready?\" and I'll "
        "post the download link right here in this chat."
    )
    video_id = result.scalar_one()
    await db.commit()

    refs_used = len(reference_urls) if used_refs else 0
    log.info(
        "cinematic video job started id=%s heygen=%s project=%s by=%s for=%s look=%r avatar=%s refs=%d aspect=%s tg=%s",
        video_id, heygen_video_id, project.id, user_id, target_name, chosen["look_name"],
        chosen["avatar_id"], refs_used, aspect_ratio, tg_chat_id,
    )
    return {
        "video_id": str(video_id),
        "status": "processing",
        "mode": "cinematic",
        "project_id": project.id,
        "project_name": project.name,
        "for_agent": target_name,
        "avatar_name": target_name,
        "look": chosen["look_name"],
        "spoken_line_preview": spoken_line[:260],
        "reference_photos": refs_used,
        "aspect_ratio": aspect_ratio,
        "format": ("1920x1080 landscape (16:9)" if aspect_ratio == "16:9"
                   else "1080x1080 square (1:1)" if aspect_ratio == "1:1"
                   else "1080x1920 portrait (9:16, Reels/TikTok)"),
        "duration_seconds": CINEMATIC_CLIP_SECONDS,
        "add_outro": True,
        "delivery_channel": "telegram" if on_telegram else "web",
        "message": (
            "Cinematic video generation started (Seedance) — a ~15s clip. "
            "This takes a few minutes. " + delivery
        ),
    }


registry.register(Tool(
    name="create_cinematic_video",
    description=(
        "Generate a CINEMATIC promo video (HeyGen Cinematic Avatar / Seedance) about a project. This "
        "is the NEW 'Cinematic mode' — distinct from create_promo_video (the scripted 'Avatar V5' "
        "mode). It produces a single ~15-second clip where the agent appears in a project scene and "
        "SPEAKS a short line; there is no separate narration track or AI background — the scene and "
        "speech come from the prompt plus the project's photos (auto-attached as references). "
        "Always uses the SIGNED-IN agent's OWN DEFAULT avatar — you cannot generate as another "
        "person and you do NOT choose a look (do NOT call list_avatar_looks for cinematic, and never "
        "pass a name). Agents only. Flow: resolve the project, propose a scene + a ≤~40-word spoken "
        "line and confirm it with the agent, THEN call this with project_name + scene_prompt + "
        "spoken_line. The Allegiance outro is always appended and captions are added automatically — "
        "do NOT ask about length, the look, outro, or captions for cinematic. Returns a video_id; "
        "poll with check_my_video_status."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Preferred. The EXACT project name the agent picked from search results.",
            },
            "project_id": {
                "type": "integer",
                "description": "Numeric project id — only if you already have it. Never guess it.",
            },
            "scene_prompt": {
                "type": "string",
                "description": (
                    "Natural-language description of the SCENE the agent is in (no spoken words here) "
                    "— e.g. 'walking through a bright modern office with floor-to-ceiling windows "
                    "showing the Dubai skyline at golden hour'. If omitted, a project-tailored scene "
                    "is used. Describe the setting only; the avatar's face comes from their look."
                ),
            },
            "spoken_line": {
                "type": "string",
                "description": (
                    "REQUIRED. The short line the avatar should SAY to camera — keep it to roughly "
                    "30–40 words (it's a ~15s clip). Write it from the project's real facts; never "
                    "invent numbers. Currency is spoken as 'dirhams' automatically."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["9:16", "16:9", "1:1"],
                "description": "Output shape. Defaults to 9:16 (portrait, Reels/TikTok). Use 16:9 for landscape.",
            },
        },
        "required": ["spoken_line"],
    },
    handler=create_cinematic_video_handler,
))


async def draft_video_scripts_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. This feature is only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    project, project_err = await _resolve_project(db, args)
    if project_err:
        return project_err

    scripts = await _write_campaign_scripts(project)
    if not scripts:
        return {"error": "Couldn't draft scripts just now — try again."}
    return {
        "project_id": project.id,
        "project_name": project.name,
        "count": len(scripts),
        "scripts": scripts,
        "message": (
            "Present these to the agent as Option 1 / Option 2 / Option 3 (quote each in full) "
            "and ask which to use or what to change. Do NOT generate the video yet."
        ),
    }


registry.register(Tool(
    name="draft_video_scripts",
    description=(
        "Draft three short narration-script VARIATIONS for a project's promo video so the agent "
        "can pick one before anything is generated. Call this at the SCRIPT step of the promo-video "
        "flow — after the agent has chosen an avatar look, before create_promo_video. Returns a "
        "`scripts` array (3 distinct variations in our house Hook/Value/CTA style). Present them as "
        "Option 1/2/3, quote each in full, and ask the agent which to use (or what to edit). Apply "
        "any edits yourself, confirm the final script, and only then call create_promo_video with "
        "that script. Agents only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "The EXACT project name the agent picked (e.g. 'Damac Hills'). Preferred.",
            },
            "project_id": {
                "type": "integer",
                "description": "Numeric project id, only if you already have it. Prefer project_name.",
            },
        },
        "required": [],
    },
    handler=draft_video_scripts_handler,
))


# Cap on how many looks we push as photos before asking the agent to choose (keeps a
# busy avatar group from spamming 20 photos into a chat).
MAX_LOOKS_SHOWN = 10


async def list_avatar_looks_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. This feature is only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    # Always the signed-in user's OWN avatar. Reject an explicit name that points at someone else.
    # Load the connected row once and reuse it for the guard and the resolver.
    av = await get_heygen_avatar(db, user_id)
    requested_name = (args.get("agent_name") or "").strip()
    if _agent_name_targets_other(requested_name, profile, av):
        return {"error": f"You can only view and use your OWN AI avatar, not {requested_name!r}'s. "
                         "Each agent's avatar is tied to their account."}
    project_id = args.get("project_id")
    project_name = args.get("project_name")  # carried through to create_promo_video

    looks, looks_err, meta = await _resolve_self_avatar(db, profile, user_id, av=av)
    if looks_err:
        return looks_err
    target_name = meta["display_name"]

    if len(looks) == 1:
        return {
            "status": "single_look",
            "agent_name": target_name,
            "project_id": project_id,
            "project_name": project_name,
            "look": {"name": looks[0]["look_name"]},
            "count": 1,
            "message": f"{target_name} has a single avatar look — no choice needed; you can generate directly.",
        }

    shown = looks[:MAX_LOOKS_SHOWN]

    # Push each look as a captioned photo straight into the Telegram chat (one per look).
    sent = False
    tg_chat_id = ctx.get("telegram_chat_id")
    if tg_chat_id:
        for lk in shown:
            if not lk.get("preview_url"):
                continue
            data = await _fetch_bytes(lk["preview_url"])
            if not data:
                continue
            if await _send_telegram_photo(int(tg_chat_id), data, caption=lk["look_name"]):
                sent = True

    log.info("avatar looks listed for=%s count=%d/%d telegram=%s",
             target_name, len(shown), len(looks), sent)
    return {
        "status": "looks_listed",
        "agent_name": target_name,
        "project_id": project_id,
        "project_name": project_name,
        "count": len(shown),
        "total_available": len(looks),
        "truncated": len(looks) > len(shown),
        "looks": [{"name": lk["look_name"], "preview_url": lk.get("preview_url")} for lk in shown],
        "sent_to_telegram": sent,
    }


registry.register(Tool(
    name="list_avatar_looks",
    description=(
        "List the available avatar LOOKS (appearances/outfits) for the SIGNED-IN agent's OWN HeyGen "
        "avatar so they can choose one before a promo video is generated. It ALWAYS lists the "
        "requester's own avatar — you cannot list or use another person's avatar, so never pass a "
        "name. ALWAYS call this FIRST when an agent asks to make a promo/marketing video — before "
        "create_promo_video. On Telegram it sends one preview photo per look (the look's name as the "
        "caption); on web it returns the looks with preview image URLs. Your reply should LIST THE "
        "LOOK NAMES (not the URLs) and ask which to use. If it returns status 'single_look', skip the "
        "question and call create_promo_video directly. Pass project_name (the exact name the agent "
        "picked) through so you carry the right project into create_promo_video. Agents only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "The EXACT project name the video is about — pass it through so the follow-up create_promo_video uses the right project.",
            },
            "project_id": {
                "type": "integer",
                "description": "Numeric project id, only if you already have it. Prefer project_name.",
            },
        },
        "required": [],
    },
    handler=list_avatar_looks_handler,
))


async def _latest_video_id_in_conversation(db: AsyncSession, conv_id) -> Optional[UUID]:
    """The video_id the agent most recently kicked off IN THIS conversation, read back from
    the persisted `video_job` cards. This is what scopes 'is my video ready?' to the right
    chat: a request that never actually started a job (no card was written) can't resurface
    an OLD, unrelated, already-delivered video from a previous conversation — which is exactly
    the bug where a Monte Carlo request handed back a Grove-at-Sobha-Sanctuary link."""
    if conv_id is None:
        return None
    rows = (await db.execute(
        select(AskAlphaMessage.cards)
        .where(
            AskAlphaMessage.conversation_id == conv_id,
            AskAlphaMessage.role == "assistant",
            AskAlphaMessage.cards.isnot(None),
        )
        .order_by(AskAlphaMessage.id.desc())
        .limit(50)
    )).scalars().all()
    for cards in rows:
        if not isinstance(cards, list):
            continue
        for c in cards:
            if isinstance(c, dict) and c.get("type") == "video_job" and c.get("video_id"):
                try:
                    return UUID(str(c["video_id"]))
                except (ValueError, TypeError):
                    continue
    return None


async def check_my_video_status_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    video_id_arg = args.get("video_id")
    v = None
    if video_id_arg:
        try:
            target_id = UUID(str(video_id_arg))
        except (ValueError, TypeError):
            return {"error": f"Invalid video_id: {video_id_arg}"}
        v = (await db.execute(
            select(Video).where(Video.id == target_id, Video.requested_by == user_id)
        )).scalar_one_or_none()
        if v is None:
            return {"status": "none",
                    "message": "I couldn't find a video with that id under your account."}
    else:
        # 1) The video THIS conversation actually started (scoped via the persisted card).
        conv_vid = await _latest_video_id_in_conversation(db, ctx.get("conversation_id"))
        if conv_vid is not None:
            v = (await db.execute(
                select(Video).where(Video.id == conv_vid, Video.requested_by == user_id)
            )).scalar_one_or_none()
        # 2) Safety net for a new tab / restarted session: a video of THEIRS that's still
        #    rendering is unambiguously the one they're waiting on. We deliberately do NOT
        #    fall back to an already-completed video from another conversation — that is what
        #    served a stale, wrong link before.
        if v is None:
            v = (await db.execute(
                select(Video).where(
                    Video.requested_by == user_id,
                    Video.status.in_(("pending", "processing")),
                ).order_by(Video.created_at.desc()).limit(1)
            )).scalar_one_or_none()

    if v is None:
        return {
            "status": "none",
            "message": "I don't see a video generating from this chat. Want me to start one?",
        }

    project_name = None
    if v.project_id is not None:
        project_name = (await db.execute(
            select(Project.name).where(Project.id == v.project_id)
        )).scalar_one_or_none()

    # A video is deliverable ONLY when genuinely completed AND a real URL exists. During the
    # Descript caption step the poller keeps status='processing' while the RAW url is already
    # populated — surfacing that as a finished link was the false-'ready' bug — so we gate the
    # URL on completion. Prefer the captioned version; fall back to the raw HeyGen video.
    # Precedence: captioned composite (or captioned raw) → uncaptioned b-roll composite → raw.
    # broll_video_url only wins when b-roll succeeded but captioning later failed.
    completed = v.status == "completed"
    share_url = (v.captioned_video_url or v.broll_video_url or v.video_url) if completed else None
    is_ready = completed and bool(share_url)

    result = {
        "video_id": str(v.id),
        "status": v.status,
        "ready": is_ready,
        "project_id": v.project_id,
        "project_name": project_name,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }
    if is_ready:
        # Only the deliverable URL is exposed (no raw/captioned internals), so the model has
        # nothing stale or half-finished to paste.
        result["video_url"] = share_url
        result["thumbnail_url"] = v.thumbnail_url
        result["completed_at"] = v.completed_at.isoformat() if v.completed_at else None
    elif v.status == "failed":
        result["error_detail"] = v.error or v.caption_error
    return result


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
