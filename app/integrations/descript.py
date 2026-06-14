"""Descript API client — post-process a finished video to add captions.

Official API (https://docs.descriptapi.com). Flow:
    POST /jobs/import/project_media  -> import the finished MP4, yields a project_id
    POST /jobs/agent                 -> Underlord AI adds captions (prompt-driven)
    POST /jobs/publish               -> render the composition to a downloadable video
    GET  /jobs/{job_id}              -> poll each of the above to completion
Returns the published video's signed download URL.

CAVEATS (documented limits, not bugs):
- The agent picks the caption STYLE; the API exposes no Hormozi/template selector, so the
  exact look is whatever Descript applies. settings.descript_caption_prompt is the only lever.
- Whether `publish` burns the agent's captions into the MP4 is not stated in the public docs;
  verify on a real run. If it doesn't, the captioned URL will simply look uncaptioned.

The exact request/response field names aren't fully pinned down in the public docs, so the
extraction below is defensive (documented names first, then common alternatives). If a real
run logs "no X in response", adjust the field name here against the live payload. This whole
step is best-effort — the poller falls back to the raw HeyGen video on any DescriptError.
"""
import asyncio
import logging
from typing import Any, Optional
import httpx
from app.config import settings

log = logging.getLogger("askalpha.descript")

API_BASE = "https://descriptapi.com/v1"
_POLL_INTERVAL_SEC = 5
_POLL_TIMEOUT_SEC = 600  # 10-min ceiling per job


class DescriptError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    if not settings.descript_api_token:
        raise DescriptError("DESCRIPT_API_TOKEN is not configured")
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={
            "Authorization": f"Bearer {settings.descript_api_token}",
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )


def _unwrap(payload: Any) -> dict:
    """Responses commonly nest the useful object under `data`; some are flat."""
    if isinstance(payload, dict):
        inner = payload.get("data")
        return inner if isinstance(inner, dict) else payload
    return {}


def _dig(d: dict, *keys: str) -> Optional[Any]:
    """First non-empty value among the given keys, also looking inside a `result` object."""
    sources = [d]
    if isinstance(d.get("result"), dict):
        sources.append(d["result"])
    for src in sources:
        for k in keys:
            v = src.get(k)
            if v:
                return v
    return None


async def _post(c: httpx.AsyncClient, path: str, body: dict) -> dict:
    r = await c.post(path, json=body)
    if r.status_code >= 400:
        raise DescriptError(f"{path} failed {r.status_code}: {r.text[:300]}")
    return _unwrap(r.json() or {})


async def _await_job(c: httpx.AsyncClient, started: dict, what: str) -> dict:
    """Poll GET /jobs/{id} until terminal. `started` is the create-response; returns the
    finished job payload (raises DescriptError on failure/timeout)."""
    job_id = _dig(started, "job_id", "id")
    if not job_id:
        raise DescriptError(f"{what}: no job_id in response: {str(started)[:200]}")
    waited = 0
    while waited < _POLL_TIMEOUT_SEC:
        r = await c.get(f"/jobs/{job_id}")
        if r.status_code >= 400:
            raise DescriptError(f"{what} poll {r.status_code}: {r.text[:200]}")
        job = _unwrap(r.json() or {})
        state = str(_dig(job, "status", "state") or "").lower()
        if state in ("complete", "completed", "succeeded", "success", "done", "ready"):
            return job
        if state in ("failed", "error", "cancelled", "canceled"):
            raise DescriptError(f"{what} {state}: {str(job)[:200]}")
        await asyncio.sleep(_POLL_INTERVAL_SEC)
        waited += _POLL_INTERVAL_SEC
    raise DescriptError(f"{what}: timed out after {_POLL_TIMEOUT_SEC}s")


async def caption_video(
    source_url: str, prompt: Optional[str] = None, resolution: Optional[str] = None
) -> str:
    """Add captions to the video at source_url via Descript; return the captioned MP4's
    signed download URL. Raises DescriptError on any failure."""
    prompt = (prompt or settings.descript_caption_prompt).strip()
    resolution = resolution or settings.descript_caption_resolution
    async with _client() as c:
        # 1) import the finished video AND lay it onto a composition timeline. The media
        # URL goes inside an `add_media` map keyed by its display name (a flat
        # `media_url`/`url` is rejected); `add_compositions` then places that media on a
        # timeline. Without a composition the project has no renderable content, so the
        # later publish dies with a generic "Job failed unexpectedly".
        import_started = await _post(c, "/jobs/import/project_media", {
            "project_name": "Ask Alpha promo",
            "add_media": {"promo.mp4": {"url": source_url}},
            "add_compositions": [{"name": "promo", "clips": [{"media": "promo.mp4"}]}],
        })
        project_id = _dig(import_started, "project_id", "projectId")
        if not project_id:
            raise DescriptError(f"import: no project_id: {str(import_started)[:200]}")
        imported = await _await_job(c, import_started, "import")
        comps = _dig(imported, "created_compositions") or []
        composition_id = comps[0].get("id") if (comps and isinstance(comps[0], dict)) else None
        if not composition_id:
            raise DescriptError(f"import: no composition created: {str(imported)[:200]}")

        # 2) Underlord adds captions to that composition
        await _await_job(c, await _post(c, "/jobs/agent", {
            "project_id": project_id, "composition_id": composition_id, "prompt": prompt,
        }), "agent")

        # 3) publish/render the composition to a downloadable video. access_level must be
        # one the drive permits (public/unlisted/private — NOT "drive"); "private" still
        # returns a signed download_url.
        published = await _await_job(
            c,
            await _post(c, "/jobs/publish", {
                "project_id": project_id,
                "composition_id": composition_id,
                "media_type": "Video",
                "resolution": resolution,
                "access_level": settings.descript_caption_access_level,
            }),
            "publish",
        )
        url = _dig(published, "download_url", "downloadUrl", "url", "share_url")
        if not url:
            raise DescriptError(f"publish: no download_url: {str(published)[:200]}")
        log.info("descript captioned video ready (%s)", url[:80])
        return url
