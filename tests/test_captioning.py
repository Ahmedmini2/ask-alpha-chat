"""Unit tests for the caption post-step.

Everything heavy (faster-whisper, the Remotion CLI, S3, Telegram, the DB) is
mocked, so these run with no model download, no Node, and no network. Async code
is driven with asyncio.run() to avoid a pytest-asyncio dependency.
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.captioning import pipeline as caption_pipeline
from app.captioning import storage as caption_storage
from app.captioning import transcribe as caption_transcribe
from app.workers import heygen_poller


# ----------------------------- transcribe -----------------------------

def test_transcribe_shapes_word_tokens(monkeypatch):
    words = [
        SimpleNamespace(word=" Hello", start=0.0, end=0.4),
        SimpleNamespace(word=" world", start=0.4, end=0.4),  # zero-length → guarded
        SimpleNamespace(word="   ", start=1.0, end=1.2),       # blank → dropped
    ]
    seg = SimpleNamespace(words=words)
    fake_model = SimpleNamespace(transcribe=lambda *a, **k: ([seg], object()))
    monkeypatch.setattr(caption_transcribe, "_load_model", lambda: fake_model)

    tokens = asyncio.run(caption_transcribe.transcribe("x.mp4"))
    assert tokens == [
        {"text": "Hello", "startMs": 0, "endMs": 400},
        {"text": "world", "startMs": 400, "endMs": 520},  # 400 + 120 guard
    ]


# ------------------------------- storage ------------------------------

def test_slugify():
    assert caption_storage.slugify("Marina Heights!!") == "marina-heights"
    assert caption_storage.slugify("") == "video"
    assert caption_storage.slugify(None) == "video"


def test_upload_video_key_and_content_type(monkeypatch):
    calls = {}

    class FakeS3:
        def put_object(self, **kw):
            calls.update(kw)

        def generate_presigned_url(self, op, Params, ExpiresIn):
            calls["presigned"] = (op, Params, ExpiresIn)
            return "https://signed.example/x.mp4"

    monkeypatch.setattr(caption_storage, "_s3", FakeS3())
    key, url = asyncio.run(caption_storage.upload_video(b"data", "Marina Heights"))

    assert key.startswith("generated/videos/marina-heights-")
    assert key.endswith(".mp4")
    assert url == "https://signed.example/x.mp4"
    assert calls["ContentType"] == "video/mp4"
    assert calls["Bucket"] == caption_storage.ASSETS_BUCKET


# ------------------------------- pipeline -----------------------------

def test_caption_video_raises_on_empty_transcript(monkeypatch):
    async def fake_dl(url, dest):
        return None

    async def fake_tx(path):
        return []

    monkeypatch.setattr(caption_pipeline, "_download", fake_dl)
    monkeypatch.setattr(caption_pipeline.caption_transcribe, "transcribe", fake_tx)

    with pytest.raises(caption_pipeline.CaptionError):
        asyncio.run(caption_pipeline.caption_video("https://heygen/x.mp4"))


def test_caption_video_passes_source_url_to_render(monkeypatch):
    captured = {}

    async def fake_dl(url, dest):
        return None

    async def fake_tx(path):
        return [{"text": "hi", "startMs": 0, "endMs": 100}]

    async def fake_render(src, captions):
        captured["src"] = src
        captured["captions"] = captions
        return b"OUT"

    monkeypatch.setattr(caption_pipeline, "_download", fake_dl)
    monkeypatch.setattr(caption_pipeline.caption_transcribe, "transcribe", fake_tx)
    monkeypatch.setattr(caption_pipeline.caption_render, "render_captioned", fake_render)

    out = asyncio.run(caption_pipeline.caption_video("https://heygen/x.mp4"))
    assert out == b"OUT"
    # <OffthreadVideo> gets the remote URL, not the local temp file.
    assert captured["src"] == "https://heygen/x.mp4"
    assert captured["captions"][0]["text"] == "hi"


# --------------------- poller: caption_and_finalize -------------------

def _update_values(stmt) -> dict:
    """Pull the .values() mapping out of a SQLAlchemy Update for assertions."""
    out = {}
    for col, val in stmt._values.items():
        out[getattr(col, "key", str(col))] = getattr(val, "value", val)
    return out


class FakeDB:
    def __init__(self):
        self.updates = []
        self.commits = 0

    async def execute(self, stmt):
        self.updates.append(_update_values(stmt))
        return None

    async def commit(self):
        self.commits += 1


class FakeSessionCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *a):
        return False


class TgRecorder:
    def __init__(self):
        self.videos = []
        self.notifies = []


def _wire_poller(monkeypatch, db, *, caption_video, upload_video):
    monkeypatch.setattr(heygen_poller, "AsyncSessionLocal", lambda: FakeSessionCtx(db))

    async def fake_project_name(d, p):
        return "Marina Heights"

    monkeypatch.setattr(heygen_poller, "_project_name", fake_project_name)
    monkeypatch.setattr(heygen_poller.caption_pipeline, "caption_video", caption_video)
    monkeypatch.setattr(heygen_poller.caption_storage, "upload_video", upload_video)

    rec = TgRecorder()

    async def fake_send_video(chat_id, mp4, filename, caption):
        rec.videos.append({"chat_id": chat_id, "mp4": mp4, "filename": filename})
        return True

    async def fake_notify(chat_id, text):
        rec.notifies.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr(heygen_poller, "_send_telegram_video", fake_send_video)
    monkeypatch.setattr(heygen_poller, "_notify_telegram", fake_notify)
    return rec


def test_caption_and_finalize_success(monkeypatch):
    db = FakeDB()

    async def caption_video(url):
        return b"MP4BYTES"

    async def upload_video(b, name):
        return ("generated/videos/x.mp4", "https://signed/x.mp4")

    rec = _wire_poller(monkeypatch, db, caption_video=caption_video, upload_video=upload_video)

    asyncio.run(heygen_poller.caption_and_finalize(uuid4(), "https://raw/x.mp4", 5, 999))

    vals = db.updates[-1]
    assert vals["status"] == "completed"
    assert vals["caption_status"] == "done"
    assert vals["captioned_video_url"] == "https://signed/x.mp4"
    # Captioned bytes were pushed inline + a link sent.
    assert rec.videos and rec.videos[0]["mp4"] == b"MP4BYTES"
    assert any("https://signed/x.mp4" in n["text"] for n in rec.notifies)


def test_caption_and_finalize_falls_back_to_raw_on_failure(monkeypatch):
    db = FakeDB()

    async def caption_video(url):
        raise RuntimeError("remotion exploded")

    async def upload_video(b, name):  # should never be called
        raise AssertionError("upload must not run when captioning fails")

    rec = _wire_poller(monkeypatch, db, caption_video=caption_video, upload_video=upload_video)

    asyncio.run(heygen_poller.caption_and_finalize(uuid4(), "https://raw/x.mp4", 5, 999))

    vals = db.updates[-1]
    assert vals["status"] == "completed"
    assert vals["caption_status"] == "failed"
    assert "remotion exploded" in (vals["caption_error"] or "")
    # No inline video; the agent still gets the raw HeyGen link.
    assert rec.videos == []
    assert any("https://raw/x.mp4" in n["text"] for n in rec.notifies)


def test_caption_and_finalize_s3_denied_still_delivers_via_telegram(monkeypatch):
    db = FakeDB()

    async def caption_video(url):
        return b"MP4BYTES"

    async def upload_video(b, name):
        raise RuntimeError("AccessDenied: s3:PutObject")

    rec = _wire_poller(monkeypatch, db, caption_video=caption_video, upload_video=upload_video)

    asyncio.run(heygen_poller.caption_and_finalize(uuid4(), "https://raw/x.mp4", 5, 999))

    vals = db.updates[-1]
    assert vals["caption_status"] == "done"
    assert vals["captioned_video_url"] is None
    # Inline captioned video still delivered; no link to send, and the inline send
    # succeeded so we don't fall back to the raw link.
    assert rec.videos and rec.videos[0]["mp4"] == b"MP4BYTES"
    assert rec.notifies == []
