"""Unit tests for publish_to_social. The pure helpers (_clean_platforms, _media_gap,
_normalize_schedule, ayrshare.looks_like_video) are covered directly. The handler is tested
through its branches with a faked DB session and a monkeypatched Ayrshare client — the real
Ayrshare HTTP calls are exercised live, like the other network paths in this suite."""
import uuid

import pytest

import app.tools.social as social
from app.integrations.ayrshare import looks_like_video
from app.tools.social import (
    _clean_platforms,
    _media_gap,
    _normalize_schedule,
    publish_to_social_handler,
)


@pytest.fixture(autouse=True)
def _auto_permission(monkeypatch):
    """These tests cover publish MECHANICS, not the approval gate (that's in test_social_agent.py).
    Force 'auto' so publish runs without a confirmation round-trip; the faked DB then only needs
    to answer the profile_key lookup."""
    async def fake_perm(_db, _uid):
        return "auto"
    monkeypatch.setattr(social, "get_social_permission", fake_perm)


# ------------------------------- ayrshare.looks_like_video -------------------------------

def test_looks_like_video_by_extension():
    assert looks_like_video("https://s3/clip.mp4") is True
    assert looks_like_video("https://s3/clip.MOV") is True
    assert looks_like_video("https://s3/photo.jpg") is False


def test_looks_like_video_ignores_query_string():
    # signed S3 / CloudFront links keep the real extension before the '?'
    assert looks_like_video("https://files.heygen.ai/v.mp4?Key-Pair-Id=abc&Signature=xyz") is True
    assert looks_like_video("https://s3/img.png?X-Amz-Signature=abc") is False


def test_looks_like_video_empty_safe():
    assert looks_like_video("") is False


# ----------------------------------- _clean_platforms ------------------------------------

def test_clean_platforms_lowercases_and_dedupes():
    assert _clean_platforms(["Instagram", "INSTAGRAM", "LinkedIn"]) == ["instagram", "linkedin"]


def test_clean_platforms_maps_x_to_twitter():
    assert _clean_platforms(["x"]) == ["twitter"]
    assert _clean_platforms(["X.com", "twitter"]) == ["twitter"]


def test_clean_platforms_drops_unknown():
    assert _clean_platforms(["instagram", "myspace", "facebook"]) == ["instagram", "facebook"]


def test_clean_platforms_preserves_order_and_handles_empty():
    assert _clean_platforms(["linkedin", "facebook"]) == ["linkedin", "facebook"]
    assert _clean_platforms([]) == []
    assert _clean_platforms(None) == []


# -------------------------------------- _media_gap ---------------------------------------

def test_media_gap_flags_media_required_networks_without_media():
    assert _media_gap(["instagram", "facebook"], has_media=False) == ["instagram"]
    assert _media_gap(["tiktok", "youtube", "linkedin"], has_media=False) == ["tiktok", "youtube"]


def test_media_gap_empty_when_media_present():
    assert _media_gap(["instagram", "tiktok"], has_media=True) == []


def test_media_gap_empty_for_text_friendly_networks():
    assert _media_gap(["facebook", "linkedin", "twitter"], has_media=False) == []


# ----------------------------------- _normalize_schedule ----------------------------------

def test_normalize_schedule_none_is_post_now():
    assert _normalize_schedule(None) == (None, None)
    assert _normalize_schedule("") == (None, None)


def test_normalize_schedule_passthrough_z_form():
    iso, err = _normalize_schedule("2026-06-20T14:00:00Z")
    assert err is None
    assert iso == "2026-06-20T14:00:00Z"


def test_normalize_schedule_assumes_utc_for_naive():
    iso, err = _normalize_schedule("2026-06-20T14:00:00")
    assert err is None
    assert iso == "2026-06-20T14:00:00Z"


def test_normalize_schedule_converts_offset_to_utc():
    iso, err = _normalize_schedule("2026-06-20T18:00:00+04:00")   # Dubai time
    assert err is None
    assert iso == "2026-06-20T14:00:00Z"


def test_normalize_schedule_rejects_garbage():
    iso, err = _normalize_schedule("next tuesday-ish")
    assert iso is None
    assert err and "schedule time" in err


# ------------------------------------- handler branches -----------------------------------

class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Returns canned results in call order. `boom=True` raises if used — proving a branch
    short-circuits before touching the DB."""
    def __init__(self, results=(), boom=False):
        self._results = list(results)
        self._boom = boom

    async def execute(self, *_a, **_k):
        if self._boom:
            raise AssertionError("DB should not be queried on this path")
        return _Result(self._results.pop(0))


def _ctx():
    return {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}


@pytest.mark.asyncio
async def test_no_platforms_short_circuits_before_db():
    out = await publish_to_social_handler(FakeSession(boom=True), {"caption": "hi"}, _ctx())
    assert out["status"] == "needs_input"


@pytest.mark.asyncio
async def test_media_required_without_media_short_circuits_before_db():
    out = await publish_to_social_handler(
        FakeSession(boom=True), {"platforms": ["instagram"], "caption": "hello"}, _ctx())
    assert out["status"] == "needs_media"
    assert out["needs_media"] == ["instagram"]


@pytest.mark.asyncio
async def test_bad_schedule_short_circuits_before_db():
    out = await publish_to_social_handler(
        FakeSession(boom=True),
        {"platforms": ["linkedin"], "caption": "hi", "schedule_date": "whenever"}, _ctx())
    assert out["status"] == "needs_input"


@pytest.mark.asyncio
async def test_non_https_media_rejected_before_db():
    for bad in ("ftp://x/y.mp4", "http://x/y.mp4"):  # http:// is rejected too (https-only)
        out = await publish_to_social_handler(
            FakeSession(boom=True),
            {"platforms": ["facebook"], "caption": "hi", "media_url": bad}, _ctx())
        assert out["status"] == "needs_input"


@pytest.mark.asyncio
async def test_video_only_network_with_image_short_circuits_before_db():
    # tiktok/youtube need a video, not a still image — caught before any DB/network call
    out = await publish_to_social_handler(
        FakeSession(boom=True),
        {"platforms": ["tiktok"], "caption": "hi", "media_url": "https://s3/flyer.png"}, _ctx())
    assert out["status"] == "needs_media"
    assert out["needs_video"] == ["tiktok"]


@pytest.mark.asyncio
async def test_not_connected_when_no_profile_key():
    out = await publish_to_social_handler(
        FakeSession(results=[None]), {"platforms": ["linkedin"], "caption": "hi"}, _ctx())
    assert out["status"] == "not_connected"


@pytest.mark.asyncio
async def test_anonymous_user_is_not_connected():
    ctx = {"user_id": None, "channel": "website"}
    out = await publish_to_social_handler(
        FakeSession(boom=True), {"platforms": ["linkedin"], "caption": "hi"}, ctx)
    # user_id None never hits the DB (get_profile_key returns None first)
    assert out["status"] == "not_connected"


@pytest.mark.asyncio
async def test_needs_link_when_platform_not_connected(monkeypatch):
    async def fake_linked(_pk):
        return ["facebook"]
    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]), {"platforms": ["linkedin"], "caption": "hi"}, _ctx())
    assert out["status"] == "needs_link"
    assert out["missing"] == ["linkedin"]


@pytest.mark.asyncio
async def test_publish_success_returns_post_urls(monkeypatch):
    async def fake_linked(_pk):
        return ["linkedin", "facebook"]

    async def fake_publish(profile_key, post, platforms, media_urls=None, schedule_date=None, is_video=None):
        assert profile_key == "pk_123"
        assert media_urls is None and schedule_date is None
        return {"status": "success",
                "postIds": [{"platform": "linkedin", "postUrl": "https://lnkd.in/p/1"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]),
        {"platforms": ["linkedin"], "caption": "Big news"}, _ctx())
    assert out["status"] == "published"
    assert out["post_urls"] == ["https://lnkd.in/p/1"]
    assert out["scheduled"] is False


@pytest.mark.asyncio
async def test_scheduled_post_sets_schedule_and_status(monkeypatch):
    captured = {}

    async def fake_linked(_pk):
        return ["facebook"]

    async def fake_publish(profile_key, post, platforms, media_urls=None, schedule_date=None, is_video=None):
        captured["schedule_date"] = schedule_date
        return {"status": "success", "postIds": []}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]),
        {"platforms": ["facebook"], "caption": "Later", "schedule_date": "2026-06-20T14:00:00Z"},
        _ctx())
    assert out["status"] == "scheduled"
    assert out["scheduled"] is True
    assert captured["schedule_date"] == "2026-06-20T14:00:00Z"


@pytest.mark.asyncio
async def test_video_media_url_passed_through_to_publish(monkeypatch):
    captured = {}

    async def fake_linked(_pk):
        return ["instagram"]

    async def fake_publish(profile_key, post, platforms, media_urls=None, schedule_date=None, is_video=None):
        captured["media_urls"] = media_urls
        captured["is_video"] = is_video
        return {"status": "success", "postIds": [{"postUrl": "https://instagram.com/p/x"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]),
        {"platforms": ["instagram"], "caption": "watch this",
         "media_url": "https://files.heygen.ai/v.mp4?Key-Pair-Id=abc"}, _ctx())
    assert out["status"] == "published"
    assert captured["media_urls"] == ["https://files.heygen.ai/v.mp4?Key-Pair-Id=abc"]
    assert captured["is_video"] is True   # .mp4 before the '?' is auto-detected as video


@pytest.mark.asyncio
async def test_is_video_hint_forces_flag_for_extensionless_url(monkeypatch):
    captured = {}

    async def fake_linked(_pk):
        return ["tiktok"]

    async def fake_publish(profile_key, post, platforms, media_urls=None, schedule_date=None, is_video=None):
        captured["is_video"] = is_video
        return {"status": "success", "postIds": [{"postUrl": "https://tiktok.com/@me/v/1"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]),
        {"platforms": ["tiktok"], "caption": "clip", "is_video": True,
         "media_url": "https://cdn.example/asset/abc123"}, _ctx())   # no extension
    assert out["status"] == "published"
    assert captured["is_video"] is True   # the hint forces it, and the video-only gate passes


@pytest.mark.asyncio
async def test_partial_success_reports_published_with_urls_and_errors(monkeypatch):
    """Ayrshare 200-with-status:error but some platforms succeeded → not a total failure."""
    async def fake_linked(_pk):
        return ["linkedin", "instagram"]

    async def fake_publish(profile_key, post, platforms, media_urls=None, schedule_date=None, is_video=None):
        return {"status": "error",
                "postIds": [{"platform": "linkedin", "postUrl": "https://lnkd.in/p/1"}],
                "errors": [{"platform": "instagram", "message": "aspect ratio invalid"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]),
        {"platforms": ["linkedin", "instagram"], "caption": "hi",
         "media_url": "https://s3/img.png"}, _ctx())
    assert out["status"] == "published"            # not "error" — linkedin went live
    assert out["post_urls"] == ["https://lnkd.in/p/1"]
    assert any("aspect ratio" in e for e in out["errors"])
    assert "https://lnkd.in/p/1" in out["message"]  # url folded into the message deterministically
    assert "aspect ratio" in out["message"]


@pytest.mark.asyncio
async def test_publish_failure_is_caught_and_reported(monkeypatch):
    async def fake_linked(_pk):
        return ["linkedin"]

    async def fake_publish(*_a, **_k):
        raise social.ayrshare.AyrshareError("rate limited")

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]), {"platforms": ["linkedin"], "caption": "hi"}, _ctx())
    assert out["status"] == "error"
    assert "rate limited" in out["message"]


@pytest.mark.asyncio
async def test_link_precheck_failure_does_not_block_publish(monkeypatch):
    """If the linked-platforms pre-check errors, we still attempt to publish."""
    async def fake_linked(_pk):
        raise RuntimeError("ayrshare /user down")

    async def fake_publish(*_a, **_k):
        return {"status": "success", "postIds": [{"postUrl": "https://fb/p/1"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(results=["pk_123"]), {"platforms": ["facebook"], "caption": "hi"}, _ctx())
    assert out["status"] == "published"
