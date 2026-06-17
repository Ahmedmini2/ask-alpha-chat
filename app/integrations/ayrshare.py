"""Ayrshare client — publish a post to a user's connected social accounts.

Ayrshare exposes ONE shared "Primary Profile" API key (settings.ayrshare_api_key); each end
user has a per-account `Profile-Key` (stored by the web app in public.ayrshare_profiles). Every
call here MUST send BOTH headers — the Bearer API key AND the user's `Profile-Key` — or the post
lands on the Primary Profile (the WRONG account). The profile-key lookup lives in
app.tools.social (it needs the DB session); this module is pure HTTP and holds no DB knowledge.

Error surfacing is defensive: Ayrshare can return HTTP 200 with `status: "error"`, so we treat
BOTH a >=400 status and a body-level error as failure and raise AyrshareError with the best
message we can dig out of the payload.

Docs: https://www.ayrshare.com/docs/apis/post/overview
"""
import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger("askalpha.ayrshare")

API_BASE = "https://api.ayrshare.com/api"
_TIMEOUT = 60.0

# Path extensions Ayrshare treats as video (so we set isVideo). Checked against the URL with
# its query string stripped (signed S3 / CloudFront links carry the real extension before the
# '?'), lower-cased.
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv")


class AyrshareError(Exception):
    pass


def looks_like_video(url: str) -> bool:
    """True if the URL's path (query string stripped) ends in a known video extension."""
    return (url or "").split("?")[0].lower().endswith(VIDEO_EXT)


def _headers(profile_key: str) -> dict:
    if not settings.ayrshare_api_key:
        raise AyrshareError("AYRSHARE_API_KEY is not configured")
    if not profile_key:
        raise AyrshareError("Missing Ayrshare Profile-Key for this user")
    return {
        "Authorization": f"Bearer {settings.ayrshare_api_key}",
        "Profile-Key": profile_key,
        "Content-Type": "application/json",
    }


def _error_message(data: dict, status_code: int) -> str:
    """Best human-readable error from an Ayrshare failure payload."""
    msg = data.get("message")
    if not msg:
        errs = data.get("errors") or []
        if errs and isinstance(errs[0], dict):
            msg = errs[0].get("message")
    return msg or f"Ayrshare request failed ({status_code})"


async def get_linked_platforms(profile_key: str) -> list[str]:
    """Which networks this user has actually linked, e.g. ['instagram', 'linkedin']."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{API_BASE}/user", headers=_headers(profile_key))
        r.raise_for_status()
        data = r.json()
    # Ayrshare can return HTTP 200 with status:"error" (e.g. an invalid/expired Profile-Key);
    # mirror publish()'s contract so the caller doesn't read a missing list as "nothing linked"
    # and send the user to the dashboard for what is actually an auth problem.
    if data.get("status") == "error":
        raise AyrshareError(_error_message(data, r.status_code))
    return data.get("activeSocialAccounts") or []


async def publish(
    profile_key: str,
    post: str,
    platforms: list[str],
    media_urls: Optional[list[str]] = None,
    schedule_date: Optional[str] = None,
    is_video: Optional[bool] = None,
) -> dict:
    """Publish (or, with schedule_date, schedule) a post.

    Returns the Ayrshare response body on FULL or PARTIAL success (the caller splits the live
    posts in `postIds` from the per-network failures in `errors`); raises AyrshareError only on
    a TOTAL failure (nothing went out).

    `is_video`: force the video flag — True/False to override, None (default) to auto-detect from
    the URL extension. The override is needed for extension-less / signed video URLs, which
    Ayrshare would otherwise treat as images and reject on video-only networks.
    """
    body: dict = {"post": post, "platforms": platforms}
    if media_urls:
        body["mediaUrls"] = media_urls
        if is_video is True or (is_video is None and any(looks_like_video(u) for u in media_urls)):
            body["isVideo"] = True
    if schedule_date:
        body["scheduleDate"] = schedule_date  # ISO-8601 UTC, e.g. 2026-06-20T14:00:00Z

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{API_BASE}/post", json=body, headers=_headers(profile_key))
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    # Ayrshare returns HTTP 200 with status:"error" when ANY platform fails — but on PARTIAL
    # success it still carries the live posts in `postIds`. Only treat it as a hard failure when
    # nothing went out; otherwise return the body so the caller reports what posted AND what failed.
    if r.status_code >= 400 or (data.get("status") == "error" and not data.get("postIds")):
        raise AyrshareError(_error_message(data, r.status_code))
    return data


# ----------------------------- generic read/action transport -----------------------------
# Used by the v2 social-agent tools (analytics, comments, DMs, replies). Same auth headers and
# the same HTTP-200-with-status:"error" failure contract as publish(); JSON bodies may be a dict
# (the common case) or, for some list endpoints, a bare array.

def _raise_if_error(data, status_code: int) -> None:
    if status_code >= 400 or (isinstance(data, dict) and data.get("status") == "error"):
        msg = _error_message(data, status_code) if isinstance(data, dict) else f"Ayrshare request failed ({status_code})"
        raise AyrshareError(msg)


async def _get_json(path: str, profile_key: str, params: Optional[dict] = None):
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{API_BASE}{path}", params=params, headers=_headers(profile_key))
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    _raise_if_error(data, r.status_code)
    return data


async def _post_json(path: str, profile_key: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{API_BASE}{path}", json=body, headers=_headers(profile_key))
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    _raise_if_error(data, r.status_code)
    return data


_ENVELOPE_KEYS = {"status", "id", "lastUpdated", "nextUpdate", "code", "platform", "platforms"}


def _items(data, *keys: str) -> list:
    """Pull a list out of an Ayrshare response that may be a bare array or a dict keyed by any of
    `keys` (response shapes vary across endpoints — some are flat like {"posts": [...]}, others are
    keyed by platform like {"instagram": [...]})."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        # No expected key held a list. If the dict still carries payload beyond envelope metadata,
        # the shape isn't what we assumed — log it so a silent [] is observable, not invisible.
        payload = [k for k in data if k not in _ENVELOPE_KEYS]
        if payload:
            log.debug("ayrshare response had no list under %s; other keys present=%s", keys, payload)
    return []


# ---- reads (no approval needed) ----

async def get_post_history(profile_key: str, platform: str) -> list[dict]:
    """The user's native posts on a network (newest-first), with likes/comments counts."""
    return _items(await _get_json(f"/history/{platform}", profile_key), "posts", "history")


async def get_post_analytics(profile_key: str, post_id: str, platform: str) -> dict:
    """Deep engagement metrics for one post. searchPlatformId lets us pass a native post id."""
    return await _post_json("/analytics/post", profile_key,
                            {"id": post_id, "platforms": [platform], "searchPlatformId": True})


async def get_account_analytics(profile_key: str, platforms: list[str]) -> dict:
    """Account-level stats (followers, impressions) per platform."""
    return await _post_json("/analytics/social", profile_key, {"platforms": platforms})


async def get_comments(profile_key: str, post_id: str, platform: str) -> list[dict]:
    """Comments on a post (native post id via searchPlatformId). Ayrshare nests the comments under
    the PLATFORM key (e.g. data["instagram"]); `platform` is already normalized ('twitter', not
    'x') to match. Fall back to a flat "comments" key for safety."""
    data = await _get_json(f"/comments/{post_id}", profile_key,
                           {"searchPlatformId": "true", "platform": platform})
    return _items(data, platform, "comments")


async def get_messages(profile_key: str, platform: str, conversation_id: Optional[str] = None) -> list[dict]:
    """Direct messages for a platform, or one thread when conversation_id is given. The API
    returns sender/recipient IDs, not usernames."""
    params = {"conversationId": conversation_id} if conversation_id else None
    return _items(await _get_json(f"/messages/{platform}", profile_key, params), "messages")


# ---- actions (gated by the approval setting in the tool layer) ----

async def reply_to_comment(profile_key: str, comment_id: str, platform: str, reply: str) -> dict:
    return await _post_json(f"/comments/reply/{comment_id}", profile_key,
                            {"platforms": [platform], "comment": reply, "searchPlatformId": True})


async def send_dm(profile_key: str, platform: str, recipient_id: str, message: str,
                  media_urls: Optional[list[str]] = None) -> dict:
    body: dict = {"recipientId": recipient_id, "message": message or " "}
    if media_urls:
        body["mediaUrls"] = media_urls
    return await _post_json(f"/messages/{platform}", profile_key, body)
