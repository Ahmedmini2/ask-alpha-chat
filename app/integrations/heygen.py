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


# --------------------------------------------------------------------------
# avatar "looks" (appearances of one person)
#
# An agent's avatar lives as a HeyGen *avatar group* (e.g. "Zain Ul Abdeen"),
# and the group holds one or more *looks*. Two group shapes occur in the wild:
#   - photo-avatar looks  -> {id, name, image_url, status, is_motion, ...}
#   - standard-avatar looks -> {avatar_id, avatar_name, preview_image_url, type}
# We normalise both to {avatar_id, look_name, preview_url, is_photo}.
# --------------------------------------------------------------------------

async def list_avatar_groups() -> list[dict]:
    async with _client() as c:
        r = await c.get("/v2/avatar_group.list")
        if r.status_code >= 400:
            raise HeyGenError(f"avatar_group.list failed {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or {}
        return data.get("avatar_group_list") or []


async def list_group_looks(group_id: str) -> list[dict]:
    async with _client() as c:
        r = await c.get(f"/v2/avatar_group/{group_id}/avatars")
        if r.status_code >= 400:
            raise HeyGenError(f"group looks failed {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or {}
        return data.get("avatar_list") or data.get("avatars") or []


def _friendly_look_name(raw_name: Optional[str], person: str) -> str:
    """Human label for a look. HeyGen photo-avatar looks already carry curated names
    ('Dubai Executive'), so we mostly pass through; a look that's just the person's
    name (or unnamed) becomes 'Original'."""
    raw = " ".join((raw_name or "").split())
    if not raw or _normalize_name(raw) == _normalize_name(person):
        return "Original"
    return raw


def _normalize_look(look: dict, person: str) -> Optional[dict]:
    """Map either group-look shape to our common form; None if not generation-ready."""
    avatar_id = look.get("avatar_id") or look.get("id")
    if not avatar_id:
        return None
    # photo-avatar looks report a status; skip any that aren't finished training.
    status = look.get("status")
    if status is not None and status not in ("completed", "ready", "success"):
        return None
    is_photo = bool(look.get("id") and not look.get("avatar_id"))
    return {
        "avatar_id": avatar_id,
        "look_name": _friendly_look_name(look.get("name") or look.get("avatar_name"), person),
        "preview_url": look.get("image_url") or look.get("preview_image_url"),
        "is_photo": is_photo,
        # Best-effort: the voice HeyGen has paired with this look, when the group API
        # returns one. Lets us use the avatar's own voice instead of guessing by name.
        "default_voice_id": look.get("default_voice_id") or look.get("voice_id") or None,
    }


def _match_group(groups: list[dict], agent_name: str) -> Optional[dict]:
    """Pick the avatar group for this person: exact name, else first-name token,
    else a containment match. Among ties, prefer the one with the most looks."""
    t = _normalize_name(agent_name)
    if not t:
        return None
    t0 = t.split()[0]

    def key(g: dict) -> int:
        return g.get("num_looks") or 0

    exact = [g for g in groups if _normalize_name(g.get("name", "")) == t]
    if exact:
        return max(exact, key=key)
    first = [g for g in groups if _normalize_name(g.get("name", "")).split()[:1] == [t0]]
    if first:
        return max(first, key=key)
    contains = [g for g in groups
                if t in _normalize_name(g.get("name", "")) or _normalize_name(g.get("name", "")) in t]
    return max(contains, key=key) if contains else None


def _dedupe_look_names(looks: list[dict]) -> list[dict]:
    """Disambiguate duplicate display names so typed-name selection stays unambiguous."""
    seen: dict[str, int] = {}
    for lk in looks:
        base = lk["look_name"]
        n = seen.get(base.lower(), 0) + 1
        seen[base.lower()] = n
        if n > 1:
            lk["look_name"] = f"{base} ({n})"
    return looks


async def list_looks_for(agent_name: str) -> list[dict]:
    """All selectable looks for a person. Prefers the matching avatar group; falls back
    to the flat standard avatar (today's behaviour) when there is no group."""
    person = (agent_name or "").strip()
    if not person:
        return []
    looks: list[dict] = []
    try:
        group = _match_group(await list_avatar_groups(), person)
        if group and group.get("id"):
            for raw in await list_group_looks(group["id"]):
                norm = _normalize_look(raw, person)
                if norm and norm["avatar_id"] not in {l["avatar_id"] for l in looks}:
                    looks.append(norm)
    except HeyGenError:
        looks = []  # fall back to the flat avatar below

    if not looks:
        # No group (or empty): use the flat standard avatar, matching the old resolver
        # (full name, then first word — "Zain Ul Abdeen" -> "Zain").
        for cand in ([person] + ([person.split()[0]] if len(person.split()) > 1 else [])):
            a = await find_avatar_by_name(cand)
            if a:
                looks = [{
                    "avatar_id": a["avatar_id"],
                    "look_name": _friendly_look_name(a.get("avatar_name"), person),
                    "preview_url": a.get("preview_image_url"),
                    "is_photo": False,
                }]
                break
    return _dedupe_look_names(looks)


def _match_own_group(groups: list[dict], identity_names: set[str]) -> Optional[dict]:
    """Pick the avatar group belonging to the SIGNED-IN user — SAFELY. Unlike _match_group (a
    free-text search that matches on a shared first-name token or substring), this is used as a
    security identity resolver, so it never matches on a first name alone.

    A group qualifies only when its name is token-compatible with one of the user's own identity
    names: an exact (normalized) match, or — for a multi-token identity (e.g. 'Zain Ul Abdeen') —
    a token-subset relationship (the identity's tokens are all in the group's, or vice-versa). That
    keeps 'Zain Ul Abdeen' → 'Zain Ul Abdeen'/'Zain Ul Abdeen Official' working while REJECTING
    'Ahmed Othman' → 'Ahmed Khan' (no token-subset) and any single-token first-name collision. On
    ambiguity (more than one distinct group qualifies) it returns None rather than guess."""
    names = {_normalize_name(n) for n in identity_names}
    names.discard("")
    if not names:
        return None

    # 1) exact name equality — the common, unambiguous case (real agents store their full name).
    exact = [g for g in groups if _normalize_name(g.get("name", "")) in names]
    if exact:
        return max(exact, key=lambda g: g.get("num_looks") or 0)

    # 2) multi-token subset match, accepted ONLY when it resolves to a single group. A single-token
    #    identity ('Ahmed') is never allowed to subset-match a fuller group name (too collision-prone).
    multi = {n for n in names if len(n.split()) >= 2}
    cands: dict = {}
    for g in groups:
        gtok = set(_normalize_name(g.get("name", "")).split())
        if not gtok:
            continue
        for n in multi:
            ntok = set(n.split())
            if ntok <= gtok or gtok <= ntok:
                cands[id(g)] = g
                break
    uniq = list(cands.values())
    return uniq[0] if len(uniq) == 1 else None


async def list_looks_for_self(identity_names: set[str], person: str) -> list[dict]:
    """Looks for the SIGNED-IN user's OWN avatar, resolved by a SAFE name match (see _match_own_group)
    against the account's avatar groups, then flat standard avatars. This is the legacy fallback used
    only when the user has no connected `heygen_avatars` row; it never matches a different person who
    merely shares a first name. `identity_names` are the caller's own names (full name, email-local,
    etc.); `person` is the display label used for friendly look names."""
    names = {n for n in identity_names if n and n.strip()}
    if not names:
        return []
    looks: list[dict] = []
    try:
        group = _match_own_group(await list_avatar_groups(), names)
        if group and group.get("id"):
            for raw in await list_group_looks(group["id"]):
                norm = _normalize_look(raw, person)
                if norm and norm["avatar_id"] not in {l["avatar_id"] for l in looks}:
                    looks.append(norm)
    except HeyGenError:
        looks = []  # fall back to the flat avatar below

    if not looks:
        # No group: use a flat standard avatar, matched EXACTLY (no first-token guessing).
        norm_names = {_normalize_name(n) for n in names}
        for a in await list_avatars():
            if _normalize_name(a.get("avatar_name", "")) in norm_names:
                looks = [{
                    "avatar_id": a["avatar_id"],
                    "look_name": _friendly_look_name(a.get("avatar_name"), person),
                    "preview_url": a.get("preview_image_url"),
                    "is_photo": False,
                    "default_voice_id": a.get("default_voice_id") or a.get("voice_id") or None,
                }]
                break
    return _dedupe_look_names(looks)


async def looks_for_connected_avatar(
    group_id: Optional[str],
    avatar_id: Optional[str],
    preview_url: Optional[str],
    person: str,
) -> list[dict]:
    """All selectable looks for a user's OWN connected avatar (a `heygen_avatars` row).

    Unlike list_looks_for(), this never name-matches across the account — it reads ONLY the
    given avatar group, so the result is provably locked to this person's avatar. The known
    `avatar_id` is guaranteed to appear as a look even if the group API returns nothing (e.g. a
    freshly-created instant/photo twin that hasn't surfaced in avatar_group/{id}/avatars yet).
    """
    looks: list[dict] = []
    if group_id:
        try:
            for raw in await list_group_looks(group_id):
                norm = _normalize_look(raw, person)
                if norm and norm["avatar_id"] not in {l["avatar_id"] for l in looks}:
                    looks.append(norm)
        except HeyGenError:
            looks = []  # fall back to the bare avatar_id below
    if avatar_id and avatar_id not in {l["avatar_id"] for l in looks}:
        # Digital twins from a recorded video are photo (talking_photo) avatars; is_photo=True
        # makes generate_video try the talking_photo shape first and fall back to the avatar
        # shape, so this works whichever kind HeyGen actually created.
        looks.insert(0, {
            "avatar_id": avatar_id,
            "look_name": _friendly_look_name(person, person),  # -> "Original"
            "preview_url": preview_url,
            "is_photo": True,
            "default_voice_id": None,
        })
    return _dedupe_look_names(looks)


def find_look_in(looks: list[dict], look_query: str) -> Optional[dict]:
    """Resolve a typed look name against an ALREADY-resolved look list (so the caller stays in
    control of which avatar the looks came from). Exact (case-insensitive), then substring, then
    token overlap — same precedence as find_look()."""
    q = _normalize_name(look_query)
    if not q or not looks:
        return None
    for lk in looks:
        if _normalize_name(lk["look_name"]) == q:
            return lk
    for lk in looks:
        ln = _normalize_name(lk["look_name"])
        if q in ln or ln in q:
            return lk
    qtok = set(q.split())
    best, best_overlap = None, 0
    for lk in looks:
        overlap = len(qtok & set(_normalize_name(lk["look_name"]).split()))
        if overlap > best_overlap:
            best, best_overlap = lk, overlap
    return best


async def find_look(agent_name: str, look_query: str) -> Optional[dict]:
    """Resolve a typed look name back to a concrete look. Exact (case-insensitive)
    first, then substring, then token overlap."""
    q = _normalize_name(look_query)
    looks = await list_looks_for(agent_name)
    if not q or not looks:
        return None
    for lk in looks:
        if _normalize_name(lk["look_name"]) == q:
            return lk
    for lk in looks:
        ln = _normalize_name(lk["look_name"])
        if q in ln or ln in q:
            return lk
    qtok = set(q.split())
    best, best_overlap = None, 0
    for lk in looks:
        overlap = len(qtok & set(_normalize_name(lk["look_name"]).split()))
        if overlap > best_overlap:
            best, best_overlap = lk, overlap
    return best


async def generate_video(
    script: str,
    avatar_id: str,
    voice_id: str,
    background_url: Optional[str] = None,
    engine: str = "avatar_v",
    resolution: str = "1080p",
    aspect_ratio: str = "9:16",
    caption: bool = True,
    is_photo: bool = False,
    **_legacy_kwargs,  # accept (and ignore) old width/height kwargs
) -> str:
    """Submit a video job via the v3 /v3/videos endpoint.

    `engine` selects HeyGen's avatar engine — "avatar_v" (newest, full background swap),
    "avatar_iv" (default v4), or omit to use the avatar's default. If the caller's chosen
    engine isn't supported by the avatar (HeyGen returns 400/422), we retry once with
    "avatar_iv" then with no engine.

    `is_photo` selects how the character is addressed: a HeyGen *photo-avatar look*
    (`type: talking_photo`, `talking_photo_id`) vs a standard avatar (`type: avatar`,
    `avatar_id`). For a photo look we try the talking_photo shape first and fall back to
    the avatar shape — rejected attempts cost nothing (no job is created on a 4xx).

    Returns the HeyGen video_id.
    """
    base_payload: dict = {
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

    # How to address the character. For a photo look, the talking_photo shape is the
    # likely-correct one; the avatar shape is a safety net (and vice-versa is unnecessary
    # for standard avatars, which keep exactly the old single-shape behaviour).
    if is_photo:
        char_variants = [("talking_photo", "talking_photo_id"), ("avatar", "avatar_id")]
    else:
        char_variants = [("avatar", "avatar_id")]

    engines_to_try = [engine, "avatar_iv", None]
    last_err: Optional[str] = None
    async with _client() as c:
        for ctype, id_field in char_variants:
            seen: set[Optional[str]] = set()
            for eng in engines_to_try:
                if eng in seen:
                    continue
                seen.add(eng)
                payload = dict(base_payload)
                payload["type"] = ctype
                payload[id_field] = avatar_id
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
                last_err = f"{ctype}/{eng} {r.status_code}: {body}"
                # Only keep trying on recoverable (engine / 4xx-validation) errors; bubble
                # up anything else (auth, 5xx) immediately.
                if "engine" not in body.lower() and r.status_code not in (400, 422):
                    raise HeyGenError(f"v3 generate failed {last_err}")
    raise HeyGenError(f"v3 generate failed across variants: {last_err}")


async def generate_cinematic_video(
    prompt: str,
    avatar_ids: list[str],
    reference_urls: Optional[list[str]] = None,
    aspect_ratio: str = "9:16",
    resolution: str = "1080p",
    duration: int = 15,
    title: Optional[str] = None,
) -> str:
    """Submit a Cinematic Avatar (Seedance) job via /v3/videos (type 'cinematic_avatar').

    Unlike generate_video() there is NO script or voice: motion and speech are driven entirely by
    `prompt` plus the avatar look(s) and `reference_urls` (project photos). `avatar_ids` are 1–3 of
    the caller's OWN avatar look ids used as visual references; `reference_urls` are publicly
    fetchable image URLs (we presign project photos). Combined HeyGen limit: ≤9 images and ≤3 videos
    across avatars + references. Returns the HeyGen video_id (poll it with get_video_status, same as
    avatar videos — it's a normal /v3/videos id).
    """
    ids = [a for a in (avatar_ids or []) if a]
    if not ids:
        raise HeyGenError("generate_cinematic_video requires at least one avatar look id")
    payload: dict = {
        "type": "cinematic_avatar",
        "prompt": (prompt or "")[:10000],
        "avatar_id": ids[:3],                 # API accepts 1–3 look ids
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration": duration,
    }
    if reference_urls:
        payload["references"] = [{"type": "url", "url": u} for u in reference_urls if u]
    if title:
        payload["title"] = title
    async with _client() as c:
        r = await c.post("/v3/videos", json=payload)
        if r.status_code >= 400:
            raise HeyGenError(f"cinematic generate failed {r.status_code}: {r.text[:400]}")
        data = r.json().get("data") or {}
        vid = data.get("video_id")
        if not vid:
            raise HeyGenError(f"no video_id in cinematic response: {r.text[:300]}")
        return vid


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
