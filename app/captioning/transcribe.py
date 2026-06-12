"""Word-level transcription with faster-whisper.

Produces the caption tokens the Remotion composition expects: a flat list of
{text, startMs, endMs}. The model is loaded once (lazily) and reused; transcription
runs in a worker thread so it never blocks the event loop.
"""
import asyncio
import logging

from app.config import settings

log = logging.getLogger("askalpha.captioning")

_model = None
_model_lock = asyncio.Lock()


def _load_model():
    """Load (once) and return the faster-whisper model. CPU + int8 keeps memory
    bounded on the small Railway instance; the model file is cached on disk after
    the first download (~/.cache/huggingface)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        size = settings.caption_model_size or "base"
        log.info("loading faster-whisper model size=%s (cpu/int8)", size)
        _model = WhisperModel(size, device="cpu", compute_type="int8")
    return _model


def _transcribe_sync(path: str) -> list[dict]:
    model = _load_model()
    segments, _info = model.transcribe(
        path,
        word_timestamps=True,
        vad_filter=True,  # skip silence so word timings hug the speech
        beam_size=1,
    )
    tokens: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if not text:
                continue
            start_ms = int(round((w.start or 0.0) * 1000))
            end_ms = int(round((w.end or 0.0) * 1000))
            if end_ms <= start_ms:
                end_ms = start_ms + 120  # guard against zero-length tokens
            tokens.append({"text": text, "startMs": start_ms, "endMs": end_ms})
    return tokens


async def transcribe(path: str) -> list[dict]:
    """Transcribe a local media file to word tokens [{text, startMs, endMs}]."""
    async with _model_lock:  # one load/transcribe at a time (memory + thread safety)
        tokens = await asyncio.to_thread(_transcribe_sync, path)
    log.info("transcribed %s → %d word tokens", path, len(tokens))
    return tokens
