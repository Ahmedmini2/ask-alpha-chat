"""FAL (fal.ai) client — word-level transcription for burned-in captions.

We use `fal-ai/whisper` with chunk_level='word' to get per-word start/end timings, which drive
the Hormozi caption burn-in (app/videos/captions.py). FAL accepts mp4 URLs directly, so we pass
the finished HeyGen video URL as the audio source — no separate audio extraction.

Queue API (https://fal.ai/docs/model-endpoints/queue):
    POST   https://queue.fal.run/fal-ai/whisper                      -> {request_id, ...}
    GET    https://queue.fal.run/fal-ai/whisper/requests/{id}/status -> {status: IN_QUEUE|IN_PROGRESS|COMPLETED}
    GET    https://queue.fal.run/fal-ai/whisper/requests/{id}        -> {text, chunks:[{timestamp:[s,e], text}]}
Auth header: `Authorization: Key <FAL_KEY>`.
"""
import asyncio
import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger("askalpha.fal")

_MODEL = "fal-ai/whisper"
_QUEUE_BASE = "https://queue.fal.run"
_POLL_INTERVAL_SEC = 3


class FalError(Exception):
    pass


def _words_from_result(payload: dict) -> list[dict]:
    """Map a fal-ai/whisper result to [{text, start, end}] word dicts (pure; unit-tested).
    Drops chunks with missing text or timestamps."""
    out: list[dict] = []
    for ch in (payload or {}).get("chunks") or []:
        if not isinstance(ch, dict):
            continue
        ts = ch.get("timestamp") or ch.get("timestamps") or []
        text = (ch.get("text") or "").strip()
        if not text or not isinstance(ts, (list, tuple)) or len(ts) < 2:
            continue
        start, end = ts[0], ts[1]
        if start is None or end is None:
            continue
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        out.append({"text": text, "start": start, "end": end})
    return out


def _headers() -> dict:
    if not settings.fal_key:
        raise FalError("FAL_KEY is not configured")
    return {"Authorization": f"Key {settings.fal_key}", "Content-Type": "application/json"}


async def transcribe_words(audio_url: str) -> list[dict]:
    """Transcribe the media at `audio_url` to word-level timings via fal-ai/whisper.
    Returns [{text, start, end}, ...]; raises FalError on failure."""
    body = {"audio_url": audio_url, "task": "transcribe", "chunk_level": "word", "language": None}
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{_QUEUE_BASE}/{_MODEL}", headers=_headers(), json=body)
        if r.status_code >= 400:
            raise FalError(f"submit failed {r.status_code}: {r.text[:300]}")
        sub = r.json() or {}
        req_id = sub.get("request_id")
        status_url = sub.get("status_url") or f"{_QUEUE_BASE}/{_MODEL}/requests/{req_id}/status"
        result_url = sub.get("response_url") or f"{_QUEUE_BASE}/{_MODEL}/requests/{req_id}"
        if not req_id:
            raise FalError(f"no request_id in submit response: {str(sub)[:200]}")

        waited = 0
        while waited < settings.fal_whisper_timeout_sec:
            s = await c.get(status_url, headers=_headers())
            if s.status_code >= 400:
                raise FalError(f"status {s.status_code}: {s.text[:200]}")
            state = str((s.json() or {}).get("status") or "").upper()
            if state == "COMPLETED":
                break
            if state in ("FAILED", "ERROR", "CANCELLED"):
                raise FalError(f"whisper {state}: {s.text[:200]}")
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            waited += _POLL_INTERVAL_SEC
        else:
            raise FalError(f"timed out after {settings.fal_whisper_timeout_sec}s")

        res = await c.get(result_url, headers=_headers())
        if res.status_code >= 400:
            raise FalError(f"result {res.status_code}: {res.text[:200]}")
        words = _words_from_result(res.json() or {})
        if not words:
            raise FalError("whisper returned no word timings")
        log.info("fal whisper: %d words from %s", len(words), audio_url[:60])
        return words
