"""Alpha's social-media tools — read (posts/analytics/comments/DMs) and act (publish, reply to a
comment, send a DM) on the user's OWN connected accounts via Ayrshare.

Each user has one Ayrshare Profile-Key, stored by the web app in public.ayrshare_profiles
(user_id -> profile_key, where user_id == the Supabase auth id == profiles.id). We read it
straight from Postgres on the existing session — the backend's `postgres` role has BYPASSRLS,
so it reads that RLS-locked table directly, exactly like every other tool (no Supabase
service-role REST call). Every Ayrshare call sends the shared API key PLUS that Profile-Key,
scoping it to THIS user's linked accounts. (The /v1/chat JWT auth — see app/core/auth.py — is
what makes ctx['user_id'] trustworthy, so a user only ever reads/acts on their own accounts.)

Approval model (action tools only — reads always run):
- The per-user setting public.ask_alpha_settings.social_tool_permission is 'auto' | 'ask' | 'deny'
  (default 'ask'). _approval_gate enforces it, re-reading it live at execution time:
    deny -> never execute; ask + not confirmed -> return pending_confirmation (show the draft,
    no side effect) and wait for the user's yes; auto, or ask + confirmed=true -> execute.
  The model presents the draft and, after the user says yes, re-calls the tool with confirmed=true.

Every handler returns a structured dict (status + a human `message`) and NEVER raises — failures
come back as a dict the model can read out.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AyrshareProfile, AskAlphaSettings
from app.integrations import ayrshare
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.social")

# Per-network read/DM tools work with a single platform id; X is "twitter" in Ayrshare.
_PLATFORM_ALIASES = {"x": "twitter", "x.com": "twitter", "twitter.com": "twitter"}
# DMs/threads are only supported by Ayrshare on these networks.
DM_PLATFORMS = ["instagram", "facebook", "twitter"]
# Cap list-shaped read results so a chatty account can't blow the model's context.
_READ_CAP = 20


def _norm_platform(raw) -> Optional[str]:
    p = str(raw or "").strip().lower()
    p = _PLATFORM_ALIASES.get(p, p)
    return p or None

# Ayrshare network identifiers the tool accepts. NOTE: X is "twitter" in Ayrshare's API.
SUPPORTED_PLATFORMS = [
    "instagram", "facebook", "linkedin", "tiktok", "youtube", "pinterest",
    "threads", "bluesky", "telegram", "reddit", "gmb", "twitter",
]
# Networks that REJECT a text-only post — they require at least one image or video.
MEDIA_REQUIRED = {"instagram", "tiktok", "youtube", "pinterest"}
# Networks that accept ONLY video, not a still image.
VIDEO_ONLY = {"tiktok", "youtube"}
MAX_CAPTION = 5000  # generous ceiling; per-network length limits are enforced by Ayrshare


def _clean_platforms(raw) -> list[str]:
    """Lower-case, de-alias ('x' -> 'twitter'), drop unknowns, de-dupe, preserve order."""
    out: list[str] = []
    seen: set[str] = set()
    for p in (raw or []):
        p = str(p).strip().lower()
        if p in ("x", "x.com", "twitter.com"):
            p = "twitter"
        if p in SUPPORTED_PLATFORMS and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _media_gap(platforms: list[str], has_media: bool) -> list[str]:
    """Networks among `platforms` that require media but have none (sorted, for a stable msg)."""
    if has_media:
        return []
    return sorted(set(platforms) & MEDIA_REQUIRED)


def _normalize_schedule(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(iso_utc, error_message). None raw -> (None, None) = post now. Accepts any ISO-8601
    (with or without a trailing 'Z' or offset) and returns the UTC 'Z' form Ayrshare wants.
    Unparseable input -> (None, friendly error)."""
    if not raw:
        return None, None
    s = str(raw).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None, ("I couldn't read that schedule time. Tell me a date and time like "
                      "'June 20 2026, 2pm' and I'll schedule it.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None


async def get_profile_key(db: AsyncSession, user_id) -> Optional[str]:
    """The user's Ayrshare Profile-Key, or None if they've never connected accounts (or are
    anonymous). Read directly from public.ayrshare_profiles on our BYPASSRLS connection."""
    if user_id is None:
        return None
    if not isinstance(user_id, UUID):
        try:
            user_id = UUID(str(user_id))
        except (ValueError, TypeError):
            return None
    return (await db.execute(
        select(AyrshareProfile.profile_key).where(AyrshareProfile.user_id == user_id)
    )).scalar_one_or_none() or None


_NOT_CONNECTED = ("You haven't connected any social accounts yet. Open the Ask Alpha dashboard, "
                  "go to Connectors to link your accounts, then ask me again.")

_PERMISSIONS = {"auto", "ask", "deny"}


async def get_social_permission(db: AsyncSession, user_id) -> str:
    """The user's social-action approval mode: 'auto' | 'ask' | 'deny'. Defaults to 'ask' (the
    safe middle) for an unknown user, a missing row, an unexpected value, or any read failure —
    never silently 'auto' (would act without consent) or 'deny' (would block legit use)."""
    if user_id is None:
        return "ask"
    if not isinstance(user_id, UUID):
        try:
            user_id = UUID(str(user_id))
        except (ValueError, TypeError):
            return "ask"
    try:
        val = (await db.execute(
            select(AskAlphaSettings.social_tool_permission).where(AskAlphaSettings.user_id == user_id)
        )).scalar_one_or_none()
    except Exception as e:
        log.warning("ask_alpha_settings read failed for %s: %s", user_id, e)
        return "ask"
    return val if val in _PERMISSIONS else "ask"


async def _approval_gate(db: AsyncSession, ctx: dict, tool_name: str, draft: dict,
                         confirmed: bool) -> Optional[dict]:
    """Enforce the approval setting for an ACTION tool. Returns a dict to SHORT-CIRCUIT the call
    (deny / pending_confirmation), or None to proceed with execution. Permission is read live so
    a Settings change takes effect immediately, including on the confirmation turn."""
    perm = await get_social_permission(db, ctx.get("user_id"))
    if perm == "deny":
        return {"status": "denied", "action": tool_name,
                "message": ("Posting and replying are turned off in your Ask Alpha settings. "
                            "You can switch it to ask-first or automatic under Dashboard → Settings.")}
    if perm == "ask" and not confirmed:
        return {"status": "pending_confirmation", "action": tool_name, "draft": draft,
                "message": ("PENDING_CONFIRMATION — do NOT tell the user this is done; nothing has "
                            "been sent yet. Show them EXACTLY what will go out (the draft below) and "
                            f"ask them to reply 'yes' to confirm. Only when they say yes, call "
                            f"{tool_name} again with confirmed=true. Draft: {draft}")}
    return None  # 'auto', or 'ask' + confirmed


def _confirmed(args: dict) -> bool:
    return args.get("confirmed") is True


async def publish_to_social_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platforms = _clean_platforms(args.get("platforms"))
    caption = (args.get("caption") or "").strip()
    media_url = (args.get("media_url") or "").strip() or None
    schedule_raw = args.get("schedule_date")

    if not platforms:
        return {"status": "needs_input",
                "message": "Tell me which network(s) to post to (e.g. Instagram, LinkedIn)."}
    if not caption and not media_url:
        return {"status": "needs_input",
                "message": "I need a caption (or an image/video) to post."}
    if len(caption) > MAX_CAPTION:
        return {"status": "needs_input",
                "message": "That caption is too long to post — tighten it and try again."}
    if media_url and not media_url.lower().startswith("https://"):
        return {"status": "needs_input",
                "message": "The media has to be a public https link Ayrshare can download."}

    schedule_date, sched_err = _normalize_schedule(schedule_raw)
    if sched_err:
        return {"status": "needs_input", "message": sched_err}

    gap = _media_gap(platforms, bool(media_url))
    if gap:
        return {"status": "needs_media", "platforms": platforms, "needs_media": gap,
                "message": (f"{', '.join(gap)} can't post text only — they need an image or "
                            "video. Want me to attach one (for example a video you generated), "
                            "or post to the text-friendly networks instead?")}

    # Is the attached media a video? Trust an explicit hint from the model, else sniff the URL.
    # (Signed / extension-less video URLs need the hint, or Ayrshare treats them as images.)
    media_is_video = bool(media_url) and (
        args.get("is_video") is True or ayrshare.looks_like_video(media_url))
    video_only = sorted(set(platforms) & VIDEO_ONLY)
    if video_only and media_url and not media_is_video:
        return {"status": "needs_media", "platforms": platforms, "needs_video": video_only,
                "message": (f"{', '.join(video_only)} only accept a video, not a still image. "
                            "Attach a video for those, or drop them from the post.")}

    # Approval gate (publish is an ACTION). Built AFTER input validation so we never ask the user
    # to confirm an invalid post. Returns a deny / pending_confirmation dict to short-circuit.
    draft = {"platforms": platforms, "caption": caption}
    if media_url:
        draft["media_url"] = media_url
    if schedule_date:
        draft["schedule_date"] = schedule_date
    gate = await _approval_gate(db, ctx, "publish_to_social", draft, _confirmed(args))
    if gate is not None:
        return gate

    user_id = ctx.get("user_id")
    profile_key = await get_profile_key(db, user_id)
    if not profile_key:
        return {"status": "not_connected", "message": _NOT_CONNECTED}

    # Pre-check which networks are actually linked so we fail with a clear message rather than
    # a confusing Ayrshare rejection. If the check itself errors, fall through and let the
    # publish call be the source of truth.
    try:
        linked = await ayrshare.get_linked_platforms(profile_key)
    except Exception as e:
        log.warning("ayrshare get_linked_platforms failed: %s", e)
        linked = None
    if linked is not None:
        missing = [p for p in platforms if p not in linked]
        if missing:
            tail = f" You currently have {', '.join(linked)} linked." if linked else ""
            return {"status": "needs_link", "platforms": platforms, "missing": missing,
                    "linked": linked,
                    "message": (f"You haven't linked {', '.join(missing)} yet. Connect it in the "
                                f"Ask Alpha dashboard under Connectors, then ask me again." + tail)}

    try:
        result = await ayrshare.publish(
            profile_key, caption, platforms,
            media_urls=[media_url] if media_url else None,
            schedule_date=schedule_date,
            is_video=media_is_video if media_url else None,
        )
    except Exception as e:
        log.warning("ayrshare publish failed user=%s platforms=%s: %s", user_id, platforms, e)
        return {"status": "error", "platforms": platforms,
                "message": f"I couldn't publish that: {e}"}

    post_urls = [p.get("postUrl") for p in (result.get("postIds") or []) if p.get("postUrl")]
    errors = [str(x.get("message", x)) for x in (result.get("errors") or [])]
    scheduled = bool(schedule_date)
    status = "scheduled" if scheduled else "published"
    if errors and not post_urls:
        status = "error"  # nothing actually went out

    verb = "Scheduled" if scheduled else "Posted"
    msg = (f"{verb} your post to {', '.join(platforms)} for {schedule_date}."
           if scheduled else f"{verb} to {', '.join(platforms)}.")
    # Post URLs are plain (unsigned) links — fold them into the message so the user gets them
    # even if the model's prose forgets to read out post_urls.
    if post_urls:
        msg += " Live at: " + ", ".join(post_urls)
    if errors:
        msg += " Some networks reported an issue: " + "; ".join(errors)
    log.info("publish_to_social user=%s platforms=%s scheduled=%s urls=%d errors=%d",
             user_id, platforms, scheduled, len(post_urls), len(errors))
    return {
        "status": status,
        "platforms": platforms,
        "scheduled": scheduled,
        "schedule_date": schedule_date,
        "post_urls": post_urls,
        "errors": errors,
        "message": msg,
    }


registry.register(Tool(
    name="publish_to_social",
    description=(
        "Publish (or schedule) a post — a caption plus an optional image or video — to the "
        "user's OWN connected social media accounts (Instagram, Facebook, LinkedIn, X/Twitter, "
        "TikTok, YouTube, Pinterest, Threads, Bluesky, Telegram, Reddit, Google Business). "
        "Call this ONLY when the user has EXPLICITLY asked to post/publish/share to their "
        "social media AND has confirmed the final caption and target platform(s). Never post on "
        "your own initiative. To attach media the user generated (e.g. a promo video), pass its "
        "public https URL as media_url. To schedule instead of posting now, pass schedule_date as "
        "an ISO-8601 UTC time. Instagram, TikTok, YouTube and Pinterest require media — a text-only "
        "post to those is rejected. ACTION TOOL: it obeys the user's approval setting — if it "
        "returns status 'pending_confirmation', show the user the draft and get a 'yes', then call "
        "again with confirmed=true; if 'denied', relay that it's off in Settings."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "platforms": {
                "type": "array",
                "items": {"type": "string", "enum": SUPPORTED_PLATFORMS},
                "description": "Networks to post to. Use 'twitter' for X.",
            },
            "caption": {"type": "string", "description": "The post text/caption."},
            "media_url": {
                "type": "string",
                "description": ("Optional PUBLIC https URL of an image or video to attach (e.g. a "
                                "video Alpha generated). Must be directly downloadable."),
            },
            "is_video": {
                "type": "boolean",
                "description": ("Set true when media_url is a VIDEO whose link does NOT end in a "
                                "video extension (.mp4/.mov etc.), so it isn't mistaken for an "
                                "image. Leave unset for images or normal .mp4 links."),
            },
            "schedule_date": {
                "type": "string",
                "description": ("Optional ISO-8601 UTC datetime to schedule the post, e.g. "
                                "2026-06-20T14:00:00Z. Omit to post immediately."),
            },
            "confirmed": {
                "type": "boolean",
                "description": ("Set true ONLY after the user has explicitly approved this exact "
                                "post in the conversation. Leave false/unset on the first call so "
                                "the approval gate can ask them to confirm."),
            },
        },
        "required": ["platforms", "caption"],
    },
    handler=publish_to_social_handler,
))


# ============================ v2: read tools (no approval needed) ============================

async def _resolve_pk(db: AsyncSession, ctx: dict):
    """(profile_key, error_dict). error_dict is a ready-to-return 'not_connected' dict if the user
    has no Ayrshare profile (anonymous or never linked); otherwise None."""
    pk = await get_profile_key(db, ctx.get("user_id"))
    if not pk:
        return None, {"status": "not_connected", "message": _NOT_CONNECTED}
    return pk, None


async def list_posts_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platform = _norm_platform(args.get("platform"))
    if not platform:
        return {"status": "needs_input", "message": "Which network's posts should I look at?"}
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    try:
        posts = await ayrshare.get_post_history(pk, platform)
    except Exception as e:
        log.warning("list_posts %s failed: %s", platform, e)
        return {"status": "error", "platform": platform, "message": f"Couldn't read your {platform} posts: {e}"}
    return {"status": "ok", "platform": platform, "count": len(posts), "posts": posts[:_READ_CAP]}


async def get_post_analytics_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platform = _norm_platform(args.get("platform"))
    post_id = (args.get("post_id") or "").strip()
    if not platform or not post_id:
        return {"status": "needs_input", "message": "I need the post and its platform to pull analytics."}
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    try:
        data = await ayrshare.get_post_analytics(pk, post_id, platform)
    except Exception as e:
        log.warning("get_post_analytics %s failed: %s", platform, e)
        return {"status": "error", "platform": platform, "message": f"Couldn't pull analytics for that post: {e}"}
    return {"status": "ok", "platform": platform, "post_id": post_id, "analytics": data}


async def get_account_analytics_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platforms = [p for p in (_norm_platform(x) for x in (args.get("platforms") or [])) if p]
    if not platforms:
        return {"status": "needs_input", "message": "Which account(s) should I pull stats for?"}
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    try:
        data = await ayrshare.get_account_analytics(pk, platforms)
    except Exception as e:
        log.warning("get_account_analytics %s failed: %s", platforms, e)
        return {"status": "error", "platforms": platforms, "message": f"Couldn't pull account analytics: {e}"}
    return {"status": "ok", "platforms": platforms, "analytics": data}


async def get_comments_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platform = _norm_platform(args.get("platform"))
    post_id = (args.get("post_id") or "").strip()
    if not platform or not post_id:
        return {"status": "needs_input", "message": "I need the post and its platform to read comments."}
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    try:
        comments = await ayrshare.get_comments(pk, post_id, platform)
    except Exception as e:
        log.warning("get_comments %s failed: %s", platform, e)
        return {"status": "error", "platform": platform, "message": f"Couldn't read comments on that post: {e}"}
    return {"status": "ok", "platform": platform, "post_id": post_id,
            "count": len(comments), "comments": comments[:_READ_CAP]}


async def get_messages_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platform = _norm_platform(args.get("platform"))
    if platform not in DM_PLATFORMS:
        return {"status": "needs_input",
                "message": "I can read DMs on Instagram, Facebook or X. Which one?"}
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    conversation_id = (args.get("conversation_id") or "").strip() or None
    try:
        messages = await ayrshare.get_messages(pk, platform, conversation_id)
    except Exception as e:
        log.warning("get_messages %s failed: %s", platform, e)
        return {"status": "error", "platform": platform, "message": f"Couldn't read your {platform} messages: {e}"}
    return {"status": "ok", "platform": platform, "conversation_id": conversation_id,
            "count": len(messages), "messages": messages[:_READ_CAP],
            "note": "sender/recipient are platform user IDs, not usernames"}


# ============================ v2: action tools (approval-gated) ============================

async def reply_to_comment_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platform = _norm_platform(args.get("platform"))
    comment_id = (args.get("comment_id") or "").strip()
    reply = (args.get("reply") or "").strip()
    if not platform or not comment_id or not reply:
        return {"status": "needs_input",
                "message": "I need the comment, its platform, and the reply text."}
    draft = {"platform": platform, "comment_id": comment_id, "reply": reply}
    gate = await _approval_gate(db, ctx, "reply_to_comment", draft, _confirmed(args))
    if gate is not None:
        return gate
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    try:
        await ayrshare.reply_to_comment(pk, comment_id, platform, reply)
    except Exception as e:
        log.warning("reply_to_comment %s failed: %s", platform, e)
        return {"status": "error", "platform": platform, "message": f"Couldn't post the reply: {e}"}
    log.info("reply_to_comment user=%s platform=%s", ctx.get("user_id"), platform)
    return {"status": "replied", "platform": platform,
            "message": f"Replied to the comment on {platform}."}


async def send_dm_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    platform = _norm_platform(args.get("platform"))
    recipient_id = (args.get("recipient_id") or "").strip()
    message = (args.get("message") or "").strip()
    media_url = (args.get("media_url") or "").strip() or None
    if platform not in DM_PLATFORMS:
        return {"status": "needs_input", "message": "I can DM on Instagram, Facebook or X. Which one?"}
    if not recipient_id or not message:
        return {"status": "needs_input",
                "message": "I need who to message (their platform user ID from the thread) and what to say."}
    if media_url and not media_url.lower().startswith("https://"):
        return {"status": "needs_input", "message": "Any attachment has to be a public https link."}
    draft = {"platform": platform, "recipient_id": recipient_id, "message": message}
    if media_url:
        draft["media_url"] = media_url
    gate = await _approval_gate(db, ctx, "send_dm", draft, _confirmed(args))
    if gate is not None:
        return gate
    pk, err = await _resolve_pk(db, ctx)
    if err:
        return err
    try:
        await ayrshare.send_dm(pk, platform, recipient_id, message,
                               media_urls=[media_url] if media_url else None)
    except Exception as e:
        log.warning("send_dm %s failed: %s", platform, e)
        return {"status": "error", "platform": platform, "message": f"Couldn't send the DM: {e}"}
    log.info("send_dm user=%s platform=%s", ctx.get("user_id"), platform)
    return {"status": "sent", "platform": platform, "message": f"Sent your DM on {platform}."}


_READ_PLATFORMS = ["instagram", "facebook", "linkedin", "tiktok", "youtube", "pinterest", "threads", "twitter"]

registry.register(Tool(
    name="list_posts",
    description=("List the user's recent NATIVE posts on a connected platform (their real posts, "
                 "with caption, post id, likes and comment counts, newest first). Read-only — use it "
                 "to find a post before pulling its analytics or comments, e.g. 'how's my latest "
                 "Instagram post doing'."),
    input_schema={
        "type": "object",
        "properties": {"platform": {"type": "string", "enum": _READ_PLATFORMS}},
        "required": ["platform"],
    },
    handler=list_posts_handler,
))

registry.register(Tool(
    name="get_post_analytics",
    description=("Detailed engagement metrics for ONE post (impressions, reach, saves, video views, "
                 "etc. — fields vary per network). Read-only. Get the post_id from list_posts first."),
    input_schema={
        "type": "object",
        "properties": {
            "post_id": {"type": "string", "description": "Native post id (from list_posts)."},
            "platform": {"type": "string", "enum": _READ_PLATFORMS},
        },
        "required": ["post_id", "platform"],
    },
    handler=get_post_analytics_handler,
))

registry.register(Tool(
    name="get_account_analytics",
    description="Account-level stats (followers, impressions) for one or more connected platforms. Read-only.",
    input_schema={
        "type": "object",
        "properties": {
            "platforms": {"type": "array", "items": {"type": "string", "enum": _READ_PLATFORMS}},
        },
        "required": ["platforms"],
    },
    handler=get_account_analytics_handler,
))

registry.register(Tool(
    name="get_comments",
    description=("Read the comments on one of the user's posts (to gauge sentiment or find a comment "
                 "to reply to). Read-only. Get the post_id from list_posts first."),
    input_schema={
        "type": "object",
        "properties": {
            "post_id": {"type": "string", "description": "Native post id (from list_posts)."},
            "platform": {"type": "string", "enum": _READ_PLATFORMS},
        },
        "required": ["post_id", "platform"],
    },
    handler=get_comments_handler,
))

registry.register(Tool(
    name="get_messages",
    description=("Read the user's direct messages, or one DM thread, on Instagram, Facebook or X. "
                 "Read-only. Senders come back as platform user IDs, not usernames — never invent a name."),
    input_schema={
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": DM_PLATFORMS},
            "conversation_id": {"type": "string", "description": "Optional thread id to read just that conversation."},
        },
        "required": ["platform"],
    },
    handler=get_messages_handler,
))

registry.register(Tool(
    name="reply_to_comment",
    description=("ACTION (approval-gated): reply to a comment on one of the user's posts. If it "
                 "returns 'pending_confirmation', show the user the exact reply and get a 'yes', then "
                 "call again with confirmed=true. If 'denied', relay that replies are off in Settings."),
    input_schema={
        "type": "object",
        "properties": {
            "comment_id": {"type": "string", "description": "Native comment id (from get_comments)."},
            "platform": {"type": "string", "enum": _READ_PLATFORMS},
            "reply": {"type": "string", "description": "The reply text."},
            "confirmed": {"type": "boolean", "description": "True only after the user approved this exact reply."},
        },
        "required": ["comment_id", "platform", "reply"],
    },
    handler=reply_to_comment_handler,
))

registry.register(Tool(
    name="send_dm",
    description=("ACTION (approval-gated): send a direct message on Instagram, Facebook or X. The "
                 "recipient_id is the sender's platform user ID from a received message in that thread "
                 "(use get_messages first). If it returns 'pending_confirmation', show the user the "
                 "exact message and recipient and get a 'yes', then call again with confirmed=true. If "
                 "'denied', relay that DMs are off in Settings."),
    input_schema={
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": DM_PLATFORMS},
            "recipient_id": {"type": "string", "description": "Recipient's platform user ID (from get_messages)."},
            "message": {"type": "string", "description": "The message text."},
            "media_url": {"type": "string", "description": "Optional public https image/video URL to attach."},
            "confirmed": {"type": "boolean", "description": "True only after the user approved this exact DM."},
        },
        "required": ["platform", "recipient_id", "message"],
    },
    handler=send_dm_handler,
))
