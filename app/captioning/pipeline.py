"""End-to-end: HeyGen video URL → captioned MP4 bytes.

download → faster-whisper (word timestamps) → Remotion render. Raises on any
failure (including an empty transcript) so the poller can fall back to the raw
HeyGen video.
"""
import logging
import tempfile
from pathlib import Path

import httpx

from app.captioning import render as caption_render
from app.captioning import transcribe as caption_transcribe

log = logging.getLogger("askalpha.captioning")

DOWNLOAD_TIMEOUT_SEC = 120.0


class CaptionError(Exception):
    pass


async def _download(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            if r.status_code >= 400:
                raise CaptionError(f"download failed {r.status_code} for {url[:80]}")
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(64 * 1024):
                    f.write(chunk)
    if dest.stat().st_size == 0:
        raise CaptionError("downloaded an empty file")


async def caption_video(source_url: str) -> bytes:
    """Burn karaoke captions onto the video at source_url; return the MP4 bytes."""
    with tempfile.TemporaryDirectory(prefix="caption-src-") as tmp:
        src_path = Path(tmp) / "source.mp4"
        await _download(source_url, src_path)

        captions = await caption_transcribe.transcribe(str(src_path))
        if not captions:
            raise CaptionError("transcription returned no words")

        # Hand the public URL to <OffthreadVideo> (Remotion's reliable path);
        # the local copy was only needed for transcription.
        return await caption_render.render_captioned(source_url, captions)
