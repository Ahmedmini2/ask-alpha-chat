"""Thin async wrapper around HeyGen v2 API."""
from typing import Optional
import httpx
from app.config import settings

API_BASE = "https://api.heygen.com"


class HeyGenError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    if not settings.heygen_api_key:
        raise HeyGenError("HEYGEN_API_KEY is not configured")
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={
            "X-Api-Key": settings.heygen_api_key,
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )


def _normalize_name(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


async def list_avatars() -> list[dict]:
    async with _client() as c:
        r = await c.get("/v2/avatars")
        if r.status_code >= 400:
            raise HeyGenError(f"avatars failed {r.status_code}: {r.text}")
        data = r.json().get("data") or {}
        return data.get("avatars") or []


async def list_voices() -> list[dict]:
    async with _client() as c:
        r = await c.get("/v2/voices")
        if r.status_code >= 400:
            raise HeyGenError(f"voices failed {r.status_code}: {r.text}")
        data = r.json().get("data") or {}
        return data.get("voices") or []


async def find_avatar_by_name(name: str) -> Optional[dict]:
    """Case-insensitive whole-name match against the account's avatars."""
    target = _normalize_name(name)
    if not target:
        return None
    for a in await list_avatars():
        if _normalize_name(a.get("avatar_name", "")) == target:
            return a
    return None


async def find_voice_by_name(name: str) -> Optional[dict]:
    """Case-insensitive whole-name match against the account's voices."""
    target = _normalize_name(name)
    if not target:
        return None
    for v in await list_voices():
        if _normalize_name(v.get("name", "")) == target:
            return v
    return None


async def generate_video(
    script: str,
    avatar_id: str,
    voice_id: str,
    background_url: Optional[str] = None,
    engine: str = "avatar_v",
    resolution: str = "1080p",
    aspect_ratio: str = "9:16",
    caption: bool = True,
    **_legacy_kwargs,  # accept (and ignore) old width/height kwargs
) -> str:
    """Submit a video job via the v3 /v3/videos endpoint.

    `engine` selects HeyGen's avatar engine — "avatar_v" (newest, full background swap),
    "avatar_iv" (default v4), or omit to use the avatar's default. If the caller's chosen
    engine isn't supported by the avatar (HeyGen returns 400/422), we retry once with
    "avatar_iv" then with no engine.

    Returns the HeyGen video_id.
    """
    base_payload: dict = {
        "type": "avatar",
        "avatar_id": avatar_id,
        "script": script,
        "voice_id": voice_id,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
    }
    if background_url:
        base_payload["background"] = {"type": "image", "url": background_url}
    if caption:
        # v3 burns captions in when a caption object with a `style` is present — there is
        # NO `caption.enabled` flag. file_format/style are the only sub-fields the API
        # accepts (no position/font/size control). A sidecar SRT is always returned at
        # response.subtitle_url regardless.
        base_payload["caption"] = {"file_format": "srt", "style": "default"}

    # Try the requested engine first; on engine-related rejection, fall back.
    engines_to_try = [engine, "avatar_iv", None]
    seen: set[Optional[str]] = set()
    last_err: Optional[str] = None
    async with _client() as c:
        for eng in engines_to_try:
            if eng in seen:
                continue
            seen.add(eng)
            payload = dict(base_payload)
            if eng:
                payload["engine"] = {"type": eng}
            r = await c.post("/v3/videos", json=payload)
            if r.status_code < 400:
                data = r.json().get("data") or {}
                vid = data.get("video_id")
                if not vid:
                    raise HeyGenError(f"no video_id in v3 response: {r.text[:300]}")
                return vid
            body = r.text[:400]
            last_err = f"{r.status_code}: {body}"
            # Only fall back on engine-related errors; bubble up anything else.
            if "engine" not in body.lower() and r.status_code not in (400, 422):
                raise HeyGenError(f"v3 generate failed {last_err}")
    raise HeyGenError(f"v3 generate failed across engines: {last_err}")


async def upload_asset(image_bytes: bytes, content_type: str = "image/png") -> str:
    """Upload an image to HeyGen's asset store; returns a URL HeyGen will accept as background."""
    if not settings.heygen_api_key:
        raise HeyGenError("HEYGEN_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            "https://upload.heygen.com/v1/asset",
            headers={
                "X-Api-Key": settings.heygen_api_key,
                "Content-Type": content_type,
            },
            content=image_bytes,
        )
        if r.status_code >= 400:
            raise HeyGenError(f"asset upload failed {r.status_code}: {r.text[:300]}")
        data = (r.json() or {}).get("data") or {}
        url = data.get("url") or data.get("image_url") or data.get("file_url")
        if not url:
            raise HeyGenError(f"no URL in asset response: {r.text[:300]}")
        return url


async def generate_video_via_agent(
    prompt: str,
    avatar_id: Optional[str] = None,
    voice_id: Optional[str] = None,
    orientation: str = "portrait",
) -> tuple[str, Optional[str]]:
    """DEPRECATED — not used by create_promo_video. The Video Agent does NOT reliably
    honour a free-text scene/background description (it renders the avatar on its default
    scene), which is why backgrounds never changed. Use generate_video() with a real
    background image asset instead. Kept only as a thin wrapper for ad-hoc experiments.

    Returns (session_id, video_id). video_id may be None on first response for
    multi-turn modes; in `generate` mode it should arrive immediately.
    """
    payload: dict = {
        "prompt": prompt[:9990],
        "mode": "generate",
        "orientation": orientation,
    }
    if avatar_id:
        payload["avatar_id"] = avatar_id
    if voice_id:
        payload["voice_id"] = voice_id
    async with _client() as c:
        r = await c.post("/v3/video-agents", json=payload)
        if r.status_code >= 400:
            raise HeyGenError(f"video-agents failed {r.status_code}: {r.text[:500]}")
        data = (r.json() or {}).get("data") or {}
        session_id = data.get("session_id")
        video_id = data.get("video_id")
        if not session_id and not video_id:
            raise HeyGenError(f"no session_id/video_id in response: {r.text[:300]}")
        return session_id, video_id


async def get_agent_session(session_id: str) -> dict:
    """Look up a video-agent session, e.g. to get the video_id once it's assigned."""
    async with _client() as c:
        r = await c.get(f"/v3/video-agents/{session_id}")
        if r.status_code >= 400:
            raise HeyGenError(f"agent session failed {r.status_code}: {r.text[:300]}")
        return (r.json() or {}).get("data") or {}


async def get_video_status(heygen_video_id: str) -> dict:
    """Returns the HeyGen status payload normalised to keys our poller expects.

    Tries v3 first (for videos created via /v3/videos), then falls back to v1 for legacy IDs.
    Normalised keys: status, video_url, thumbnail_url, error.
    """
    async with _client() as c:
        # v3 first
        r = await c.get(f"/v3/videos/{heygen_video_id}")
        if r.status_code < 400:
            data = r.json().get("data") or {}
            raw_status = (data.get("status") or "").lower()
            # v3 uses: waiting | processing | completed | failed
            status_map = {"waiting": "processing", "pending": "processing"}
            return {
                "status": status_map.get(raw_status, raw_status),
                "video_url": data.get("video_url") or data.get("output_url"),
                "thumbnail_url": data.get("thumbnail_url"),
                "error": data.get("error"),
            }
        # Fall back to v1 (legacy v2-generated videos)
        r = await c.get("/v1/video_status.get", params={"video_id": heygen_video_id})
        if r.status_code >= 400:
            raise HeyGenError(f"status failed v3+v1 {r.status_code}: {r.text[:300]}")
        return r.json().get("data", {})
