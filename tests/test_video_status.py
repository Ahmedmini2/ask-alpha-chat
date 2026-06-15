"""Unit tests for check_my_video_status_handler's readiness gating — the logic that decides
whether a video counts as deliverable. The DB selection paths are exercised live (see
tests/test_video_data.py header); here we fake a session so we can pin down the states the
live DB rarely holds — most importantly the Descript captioning window, where status is
'processing' but a RAW url is already populated and must NOT be served as 'ready'."""
import uuid
import pytest

import app.tools.videos as videos
from app.tools.videos import check_my_video_status_handler


class FakeProfile:
    role = "salesagent"
    ask_alpha_access = "write"
    first_name = "Test"
    last_name = "Agent"


class FakeVideo:
    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.status = kw["status"]
        self.caption_status = kw.get("caption_status")
        self.video_url = kw.get("video_url")
        self.captioned_video_url = kw.get("captioned_video_url")
        self.thumbnail_url = kw.get("thumbnail_url")
        self.error = kw.get("error")
        self.caption_error = kw.get("caption_error")
        self.project_id = kw.get("project_id", 1)
        self.created_at = None
        self.completed_at = None


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Returns canned results in call order. For the video_id branch the handler issues
    exactly two queries: the Video lookup, then the project name."""
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *_a, **_k):
        return _Result(self._results.pop(0))


@pytest.fixture(autouse=True)
def _agent(monkeypatch):
    async def fake_get_profile(db, uid):
        return FakeProfile()
    monkeypatch.setattr(videos, "get_profile", fake_get_profile)
    monkeypatch.setattr(videos, "is_agent", lambda p: True)


async def _check(video):
    db = FakeSession([video, "Test Project"])
    ctx = {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}
    return await check_my_video_status_handler(db, {"video_id": str(video.id)}, ctx)


@pytest.mark.asyncio
async def test_captioning_window_is_not_ready_and_leaks_no_url():
    # The poller keeps status='processing' while Descript captions; the RAW url is set.
    v = FakeVideo(status="processing", caption_status="processing",
                  video_url="https://files.heygen.ai/raw.mp4")
    out = await _check(v)
    assert out["ready"] is False
    assert "video_url" not in out          # the raw, mid-caption url must NOT be handed out
    assert out["status"] == "processing"


@pytest.mark.asyncio
async def test_completed_with_captions_serves_captioned_url():
    v = FakeVideo(status="completed", caption_status="done",
                  video_url="https://files.heygen.ai/raw.mp4",
                  captioned_video_url="https://drive/captioned.mp4")
    out = await _check(v)
    assert out["ready"] is True
    assert out["video_url"] == "https://drive/captioned.mp4"   # captioned preferred


@pytest.mark.asyncio
async def test_completed_caption_failed_falls_back_to_raw():
    v = FakeVideo(status="completed", caption_status="failed",
                  video_url="https://files.heygen.ai/raw.mp4", captioned_video_url=None)
    out = await _check(v)
    assert out["ready"] is True
    assert out["video_url"] == "https://files.heygen.ai/raw.mp4"


@pytest.mark.asyncio
async def test_failed_reports_error_and_no_url():
    v = FakeVideo(status="failed", error="HeyGen reported failure")
    out = await _check(v)
    assert out["ready"] is False
    assert "video_url" not in out
    assert out["error_detail"] == "HeyGen reported failure"
