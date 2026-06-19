"""Tests for Cinematic (Seedance) video mode: the create_cinematic_video handler (own-avatar
enforcement, required spoken line, forced 15s/outro/mode, reference attachment), the heygen
payload shape, and the poller branch that skips b-roll for cinematic clips."""
import uuid
import pytest

import app.tools.videos as videos
from app.tools.videos import (
    create_cinematic_video_handler,
    _compose_cinematic_prompt,
    _project_reference_urls,
)
from app.integrations import heygen


class FakeProfile:
    def __init__(self, first_name="Zain", last_name="Ul Abdeen", email="zain@x.com",
                 role="salesagent", ask_alpha_access="write"):
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.role = role
        self.ask_alpha_access = ask_alpha_access


class FakeAvatar:
    def __init__(self, name="zain"):
        self.name = name
        self.group_id = "g1"
        self.avatar_id = "av1"
        self.status = "completed"
        self.consent_status = "accepted"
        self.preview_image_url = None
        self.error_message = None


class FakeProject:
    def __init__(self, name="Arancia Yards", district="Business Bay", city="Dubai", pid=42):
        self.name = name
        self.district = district
        self.city = city
        self.id = pid


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class FakeSession:
    """Only the insert .returning(Video.id) hits the DB here (project/avatar resolution is stubbed),
    so execute always returns the new video id and commit is a no-op."""
    def __init__(self, new_id):
        self._id = new_id

    async def execute(self, *_a, **_k):
        return _Result(self._id)

    async def commit(self):
        return None


@pytest.fixture
def _agent(monkeypatch):
    async def fake_get_profile(_db, _uid):
        return FakeProfile()
    async def fake_get_avatar(_db, _uid):
        return FakeAvatar()
    monkeypatch.setattr(videos, "get_profile", fake_get_profile)
    monkeypatch.setattr(videos, "is_agent", lambda p: True)
    monkeypatch.setattr(videos, "get_heygen_avatar", fake_get_avatar)


def _stub_pipeline(monkeypatch, *, looks=None, refs=None, capture=None):
    """Stub project + avatar resolution + reference building, and capture the heygen call."""
    looks = looks or [{"avatar_id": "av1", "look_name": "Original", "is_photo": True}]
    refs = refs if refs is not None else ["https://s3/p1.jpg", "https://s3/p2.jpg"]

    async def fake_resolve_project(_db, _args):
        return FakeProject(), None
    async def fake_resolve_self(_db, _profile, _uid, av=None):
        return looks, None, {"display_name": "Zain Ul Abdeen", "source": "connected", "avatar": av}
    async def fake_refs(_db, _project, k=4):
        return list(refs)
    async def fake_generate(prompt, avatar_ids, reference_urls=None, aspect_ratio=None,
                            resolution=None, duration=None, title=None):
        if capture is not None:
            capture.update(prompt=prompt, avatar_ids=avatar_ids, reference_urls=reference_urls,
                           aspect_ratio=aspect_ratio, resolution=resolution, duration=duration, title=title)
        return "heygen_cine_123"

    monkeypatch.setattr(videos, "_resolve_project", fake_resolve_project)
    monkeypatch.setattr(videos, "_resolve_self_avatar", fake_resolve_self)
    monkeypatch.setattr(videos, "_project_reference_urls", fake_refs)
    monkeypatch.setattr(videos.heygen, "generate_cinematic_video", fake_generate)


def _ctx():
    return {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}


# --------------------------------- authorization ---------------------------------

@pytest.mark.asyncio
async def test_cinematic_rejects_other_person(_agent):
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()), {"agent_name": "Chinoy", "spoken_line": "hi", "project_name": "X"}, _ctx())
    assert "error" in out and "OWN AI avatar" in out["error"] and "Chinoy" in out["error"]


@pytest.mark.asyncio
async def test_cinematic_requires_spoken_line(monkeypatch, _agent):
    _stub_pipeline(monkeypatch)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()), {"project_name": "Arancia Yards", "look": "Original"}, _ctx())
    assert "error" in out and "spoken_line" in out["error"]


# --------------------------------- happy path / forced fields ---------------------------------

@pytest.mark.asyncio
async def test_cinematic_forces_mode_outro_duration_and_avatar(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    new_id = uuid.uuid4()
    out = await create_cinematic_video_handler(
        FakeSession(new_id),
        {"project_name": "Arancia Yards", "look": "Original",
         "scene_prompt": "walking through a modern office", "spoken_line": "Welcome to Arancia Yards."},
        _ctx())
    # result shape
    assert out["mode"] == "cinematic"
    assert out["add_outro"] is True
    assert out["duration_seconds"] == 15
    assert out["status"] == "processing"
    assert out["video_id"] == str(new_id)
    assert out["reference_photos"] == 2
    # the heygen call: own look id, fixed 15s, default portrait, refs forwarded
    assert cap["avatar_ids"] == ["av1"]
    assert cap["duration"] == 15
    assert cap["aspect_ratio"] == "9:16"
    assert cap["resolution"] == "1080p"
    assert cap["reference_urls"] == ["https://s3/p1.jpg", "https://s3/p2.jpg"]
    assert "Welcome to Arancia Yards." in cap["prompt"]
    assert "walking through a modern office" in cap["prompt"]


@pytest.mark.asyncio
async def test_cinematic_invalid_aspect_falls_back_to_portrait(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "look": "Original", "spoken_line": "hi", "aspect_ratio": "4:5"},
        _ctx())
    assert cap["aspect_ratio"] == "9:16"   # 4:5 isn't valid for cinematic → coerced


@pytest.mark.asyncio
async def test_cinematic_landscape_passes_through(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "look": "Original", "spoken_line": "hi", "aspect_ratio": "16:9"},
        _ctx())
    assert cap["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_cinematic_aed_in_spoken_line_becomes_dirhams(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "look": "Original", "spoken_line": "Priced from AED 1.4M today."},
        _ctx())
    assert "1.4 million dirhams" in cap["prompt"]
    assert "AED" not in cap["prompt"]


# --------------------------------- pure helpers ---------------------------------

def test_compose_cinematic_prompt_includes_scene_and_line():
    p = _compose_cinematic_prompt("a marble lobby with city views", "Hello there",
                                  FakeProject(name="Sky Tower"))
    assert "a marble lobby with city views" in p
    assert 'says: "Hello there"' in p


def test_compose_cinematic_prompt_fallback_scene_uses_project():
    p = _compose_cinematic_prompt("", "Come see it", FakeProject(name="Sky Tower", district="Marina", city="Dubai"))
    assert "Sky Tower" in p and "Marina" in p
    assert 'says: "Come see it"' in p


@pytest.mark.asyncio
async def test_project_reference_urls_presigns_up_to_k_and_skips_none(monkeypatch):
    class A:
        def __init__(self, b, k):
            self.s3_bucket, self.s3_key = b, k
    images = [A("bk", f"k{i}") for i in range(6)]

    import app.brochures.data as bdata
    import app.brochures.storage as bstorage

    async def fake_gather(_db, _project):
        return images, []
    async def fake_presign(bucket, key, ttl=0):
        return None if key == "k1" else f"https://signed/{key}"
    monkeypatch.setattr(bdata, "_gather_assets", fake_gather)
    monkeypatch.setattr(bstorage, "presign_get", fake_presign)

    urls = await _project_reference_urls(object(), FakeProject(), k=4)
    # k=4 candidates (k0..k3); k1 presign returned None → dropped
    assert urls == ["https://signed/k0", "https://signed/k2", "https://signed/k3"]


# --------------------------------- heygen payload shape ---------------------------------

class _FakeResp:
    status_code = 200
    text = ""
    @staticmethod
    def json():
        return {"data": {"video_id": "v_xyz"}}


class _FakeClient:
    def __init__(self, capture):
        self._cap = capture
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_a):
        return False
    async def post(self, path, json=None):
        self._cap["path"] = path
        self._cap["json"] = json
        return _FakeResp()


@pytest.mark.asyncio
async def test_generate_cinematic_payload_shape(monkeypatch):
    cap = {}
    monkeypatch.setattr(heygen, "_client", lambda: _FakeClient(cap))
    vid = await heygen.generate_cinematic_video(
        "a prompt", ["a1", "a2", "a3", "a4"], reference_urls=["u1", "u2"],
        aspect_ratio="9:16", resolution="1080p", duration=15, title="t")
    assert vid == "v_xyz"
    assert cap["path"] == "/v3/videos"
    body = cap["json"]
    assert body["type"] == "cinematic_avatar"
    assert body["avatar_id"] == ["a1", "a2", "a3"]      # capped at 3 look ids
    assert body["duration"] == 15
    assert body["aspect_ratio"] == "9:16"
    assert body["references"] == [{"type": "url", "url": "u1"}, {"type": "url", "url": "u2"}]
    assert body["title"] == "t"


@pytest.mark.asyncio
async def test_generate_cinematic_requires_an_avatar_id(monkeypatch):
    monkeypatch.setattr(heygen, "_client", lambda: _FakeClient({}))
    with pytest.raises(heygen.HeyGenError):
        await heygen.generate_cinematic_video("p", [])


# --------------------------------- poller branch ---------------------------------

@pytest.mark.asyncio
async def test_poller_skips_broll_for_cinematic(monkeypatch):
    import app.workers.heygen_poller as poller
    calls = {"broll": 0, "finalized": 0}

    async def fake_broll(*_a, **_k):
        calls["broll"] += 1
        return None
    async def fake_finalize(*_a, **_k):
        calls["finalized"] += 1
    async def fake_outro(*_a, **_k):
        return None
    monkeypatch.setattr(poller, "_maybe_broll", fake_broll)
    monkeypatch.setattr(poller, "_finalize", fake_finalize)
    monkeypatch.setattr(poller, "_maybe_outro", fake_outro)
    monkeypatch.setattr(poller, "_captions_on", lambda: False)

    await poller._broll_caption_and_finalize(
        uuid.uuid4(), "https://x/raw.mp4", 1, None, script="hi", add_outro=True, mode="cinematic")
    assert calls["broll"] == 0          # b-roll skipped for cinematic
    assert calls["finalized"] == 1


@pytest.mark.asyncio
async def test_poller_runs_broll_for_avatar_mode(monkeypatch):
    import app.workers.heygen_poller as poller
    calls = {"broll": 0}

    async def fake_broll(*_a, **_k):
        calls["broll"] += 1
        return None
    async def fake_finalize(*_a, **_k):
        return None
    monkeypatch.setattr(poller, "_maybe_broll", fake_broll)
    monkeypatch.setattr(poller, "_finalize", fake_finalize)
    monkeypatch.setattr(poller, "_captions_on", lambda: False)

    await poller._broll_caption_and_finalize(
        uuid.uuid4(), "https://x/raw.mp4", 1, None, script="hi", add_outro=False, mode="avatar")
    assert calls["broll"] == 1          # scripted mode still runs b-roll
