"""Tests for Cinematic (Seedance) video mode: the create_cinematic_video handler (own-avatar
enforcement, required spoken line, DEFAULT-avatar selection with no look question, forced
15s/outro/mode, reference attachment + avatar-only retry), the heygen payload shape, the reference
builder (re-encode to JPEG + upload to HeyGen), and the poller branch that skips b-roll."""
import uuid
from pathlib import Path
import pytest

import app.tools.videos as videos
import app.workers.heygen_poller as poller
import app.videos.stitch as stitch
from app.tools.videos import (
    create_cinematic_video_handler,
    _compose_cinematic_prompt,
    _project_reference_urls,
    _split_script,
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
    def __init__(self, name="zain", avatar_id="av1"):
        self.name = name
        self.group_id = "g1"
        self.avatar_id = avatar_id
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
        return FakeAvatar()           # connected default avatar_id == "av1"
    monkeypatch.setattr(videos, "get_profile", fake_get_profile)
    monkeypatch.setattr(videos, "is_agent", lambda p: True)
    monkeypatch.setattr(videos, "get_heygen_avatar", fake_get_avatar)


def _stub_pipeline(monkeypatch, *, looks=None, refs=None, capture=None, calls=None, ref_calls=None):
    """Stub project + avatar resolution + reference building, and capture the heygen call(s). The
    resolver echoes the pre-fetched `av` into meta (as the real one does), so the handler's
    default-avatar selection has a connected avatar_id to prefer. `capture` gets the LAST generate
    call; `calls` (if given) collects EVERY generate call; `ref_calls` counts reference-builder calls."""
    looks = looks or [{"avatar_id": "av1", "look_name": "Original", "is_photo": True}]
    refs = refs if refs is not None else ["https://heygen/asset/1", "https://heygen/asset/2"]
    counter = {"n": 0}

    async def fake_resolve_project(_db, _args):
        return FakeProject(), None
    async def fake_resolve_self(_db, _profile, _uid, av=None):
        return looks, None, {"display_name": "Zain Ul Abdeen", "source": "connected", "avatar": av}
    async def fake_refs(_db, _project, k=4):
        if ref_calls is not None:
            ref_calls.append(1)
        return list(refs)
    async def fake_generate(prompt, avatar_ids, reference_urls=None, aspect_ratio=None,
                            resolution=None, duration=None, title=None):
        counter["n"] += 1
        rec = dict(prompt=prompt, avatar_ids=avatar_ids, reference_urls=reference_urls,
                   aspect_ratio=aspect_ratio, resolution=resolution, duration=duration, title=title)
        if capture is not None:
            capture.update(rec)
        if calls is not None:
            calls.append(rec)
        return f"heygen_cine_{counter['n']}"

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
        FakeSession(uuid.uuid4()), {"project_name": "Arancia Yards"}, _ctx())
    assert "error" in out and "spoken_line" in out["error"]


# --------------------------- default avatar (never asks for a look) ---------------------------

@pytest.mark.asyncio
async def test_cinematic_uses_connected_default_avatar_never_asks_look(monkeypatch, _agent):
    cap = {}
    # Two looks available; cinematic must pick the CONNECTED default (av1), never ask which look.
    looks = [
        {"avatar_id": "other_look", "look_name": "Studio", "is_photo": True},
        {"avatar_id": "av1", "look_name": "Original", "is_photo": True},
    ]
    _stub_pipeline(monkeypatch, looks=looks, capture=cap)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()), {"project_name": "X", "spoken_line": "hi"}, _ctx())
    assert "needs_look_choice" not in out and "error" not in out
    assert cap["avatar_ids"] == ["av1"]          # the connected DEFAULT, not looks[0]


@pytest.mark.asyncio
async def test_cinematic_falls_back_to_primary_look_when_no_connected_id(monkeypatch, _agent):
    cap = {}
    looks = [{"avatar_id": "primary", "look_name": "Original", "is_photo": False}]

    async def fake_resolve_project(_db, _a):
        return FakeProject(), None
    async def fake_resolve_self(_db, _p, _u, av=None):
        return looks, None, {"display_name": "Zain", "source": "name-match", "avatar": None}
    async def fake_refs(_db, _p, k=4):
        return []
    async def fake_generate(prompt, avatar_ids, **k):
        cap["avatar_ids"] = avatar_ids
        return "vid"
    monkeypatch.setattr(videos, "_resolve_project", fake_resolve_project)
    monkeypatch.setattr(videos, "_resolve_self_avatar", fake_resolve_self)
    monkeypatch.setattr(videos, "_project_reference_urls", fake_refs)
    monkeypatch.setattr(videos.heygen, "generate_cinematic_video", fake_generate)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()), {"project_name": "X", "spoken_line": "hi"}, _ctx())
    assert "error" not in out and cap["avatar_ids"] == ["primary"]


# --------------------------------- happy path / forced fields ---------------------------------

@pytest.mark.asyncio
async def test_cinematic_forces_mode_outro_duration_and_avatar(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    new_id = uuid.uuid4()
    out = await create_cinematic_video_handler(
        FakeSession(new_id),
        {"project_name": "Arancia Yards",
         "scene_prompt": "walking through a modern office", "spoken_line": "Welcome to Arancia Yards."},
        _ctx())
    assert out["mode"] == "cinematic"
    assert out["add_outro"] is True
    assert out["duration_seconds"] == 15
    assert out["status"] == "processing"
    assert out["video_id"] == str(new_id)
    assert out["reference_photos"] == 2
    assert cap["avatar_ids"] == ["av1"]
    assert cap["duration"] == 15
    assert cap["aspect_ratio"] == "9:16"
    assert cap["resolution"] == "1080p"
    assert cap["reference_urls"] == ["https://heygen/asset/1", "https://heygen/asset/2"]
    assert "Welcome to Arancia Yards." in cap["prompt"]
    assert "walking through a modern office" in cap["prompt"]


@pytest.mark.asyncio
async def test_cinematic_invalid_aspect_falls_back_to_portrait(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": "hi", "aspect_ratio": "4:5"}, _ctx())
    assert cap["aspect_ratio"] == "9:16"


@pytest.mark.asyncio
async def test_cinematic_landscape_passes_through(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": "hi", "aspect_ratio": "16:9"}, _ctx())
    assert cap["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_cinematic_aed_in_spoken_line_becomes_dirhams(monkeypatch, _agent):
    cap = {}
    _stub_pipeline(monkeypatch, capture=cap)
    await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": "Priced from AED 1.4M today."}, _ctx())
    assert "1.4 million dirhams" in cap["prompt"]
    assert "AED" not in cap["prompt"]


@pytest.mark.asyncio
async def test_cinematic_retries_avatar_only_when_references_rejected(monkeypatch, _agent):
    # The reported bug: HeyGen rejects the reference images' format → generation must NOT fail; it
    # retries with the avatar only and still starts.
    calls = []

    async def fake_resolve_project(_db, _a):
        return FakeProject(), None
    async def fake_resolve_self(_db, _p, _u, av=None):
        return [{"avatar_id": "av1", "look_name": "Original", "is_photo": True}], None, \
               {"display_name": "Zain", "source": "connected", "avatar": av}
    async def fake_refs(_db, _p, k=4):
        return ["https://heygen/asset/1", "https://heygen/asset/2"]
    async def fake_generate(prompt, avatar_ids, reference_urls=None, **k):
        calls.append(reference_urls)
        if reference_urls:
            raise heygen.HeyGenError("400: unsupported image format in references")
        return "vid_ok"
    monkeypatch.setattr(videos, "_resolve_project", fake_resolve_project)
    monkeypatch.setattr(videos, "_resolve_self_avatar", fake_resolve_self)
    monkeypatch.setattr(videos, "_project_reference_urls", fake_refs)
    monkeypatch.setattr(videos.heygen, "generate_cinematic_video", fake_generate)

    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()), {"project_name": "X", "spoken_line": "hi"}, _ctx())
    assert out.get("video_id") and "error" not in out
    assert out["reference_photos"] == 0                       # refs dropped on the retry
    assert calls == [["https://heygen/asset/1", "https://heygen/asset/2"], None]


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
async def test_project_reference_urls_reencodes_to_jpeg_and_uploads(monkeypatch):
    class A:
        def __init__(self, b, k):
            self.s3_bucket, self.s3_key = b, k
    images = [A("bk", f"k{i}") for i in range(6)]

    import app.brochures.data as bdata
    import app.brochures.storage as bstorage

    async def fake_gather(_db, _project):
        return images, []
    async def fake_fetch(bucket, key):
        return None if key == "k1" else b"raw-" + key.encode()       # k1 download fails
    def fake_to_jpeg(raw):
        return None if raw == b"raw-k2" else b"jpeg-" + raw          # k2 conversion fails
    uploaded = []
    async def fake_upload(jpeg, content_type="image/png"):
        uploaded.append((jpeg, content_type))
        return f"https://heygen/asset/{len(uploaded)}"
    monkeypatch.setattr(bdata, "_gather_assets", fake_gather)
    monkeypatch.setattr(bstorage, "fetch_asset_bytes", fake_fetch)
    monkeypatch.setattr(videos, "_to_jpeg", fake_to_jpeg)
    monkeypatch.setattr(videos.heygen, "upload_asset", fake_upload)

    urls = await _project_reference_urls(object(), FakeProject(), k=3)
    # candidates k0(ok) k1(fetch->None) k2(jpeg->None) k3(ok) k4(ok) → 3 uploads (k0,k3,k4)
    assert len(urls) == 3
    assert all(u.startswith("https://heygen/asset/") for u in urls)
    assert all(ct == "image/jpeg" for _, ct in uploaded)            # always JPEG (Seedance-safe)


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


# ============================ multi-clip cinematic (15/30/45s) ============================

_SCRIPT_3 = ("Welcome to Arancia Yards. Modern apartments and world-class amenities sit in "
             "the heart of Dubai. Smart design meets real investment potential. Submit your "
             "interest today. Priority access is open now. Don't miss this launch.")


@pytest.mark.asyncio
async def test_cinematic_15s_one_clip_no_segments(monkeypatch, _agent):
    calls, ref_calls = [], []
    _stub_pipeline(monkeypatch, calls=calls, ref_calls=ref_calls)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": "A short line.", "length_seconds": 15}, _ctx())
    assert out["clips"] == 1 and out["duration_seconds"] == 15
    assert len(calls) == 1                      # one HeyGen clip
    assert len(ref_calls) == 1                  # references uploaded once
    assert calls[0]["duration"] == 15


@pytest.mark.asyncio
async def test_cinematic_30s_generates_two_clips_reusing_refs(monkeypatch, _agent):
    calls, ref_calls = [], []
    _stub_pipeline(monkeypatch, calls=calls, ref_calls=ref_calls)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": _SCRIPT_3, "length_seconds": 30}, _ctx())
    assert out["clips"] == 2 and out["duration_seconds"] == 30
    assert len(calls) == 2                       # two HeyGen clips
    assert len(ref_calls) == 1                   # references uploaded ONCE, reused across clips
    # both clips share the same avatar, refs, aspect, duration
    assert all(c["avatar_ids"] == ["av1"] for c in calls)
    assert all(c["duration"] == 15 for c in calls)
    assert all(c["reference_urls"] == ["https://heygen/asset/1", "https://heygen/asset/2"] for c in calls)
    # the script is split across the two clips (different spoken lines in each prompt)
    assert calls[0]["prompt"] != calls[1]["prompt"]


@pytest.mark.asyncio
async def test_cinematic_45s_generates_three_clips(monkeypatch, _agent):
    calls = []
    _stub_pipeline(monkeypatch, calls=calls)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": _SCRIPT_3, "length_seconds": 45}, _ctx())
    assert out["clips"] == 3 and out["duration_seconds"] == 45
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_cinematic_invalid_length_defaults_to_15(monkeypatch, _agent):
    calls = []
    _stub_pipeline(monkeypatch, calls=calls)
    out = await create_cinematic_video_handler(
        FakeSession(uuid.uuid4()),
        {"project_name": "X", "spoken_line": _SCRIPT_3, "length_seconds": 60}, _ctx())
    assert out["duration_seconds"] == 15 and out["clips"] == 1 and len(calls) == 1


# ------------------------------- _split_script (pure) -------------------------------

@pytest.mark.parametrize("n", [1, 2, 3])
def test_split_script_returns_n_nonempty_parts(n):
    parts = _split_script(_SCRIPT_3, n)
    assert len(parts) == n
    assert all(p.strip() for p in parts)               # never an empty clip
    # order preserved: concatenation contains the first and last words in order
    assert parts[0].startswith("Welcome")
    assert parts[-1].endswith("launch.")


def test_split_script_no_sentence_breaks_word_splits():
    parts = _split_script("one two three four five six", 3)
    assert [len(p.split()) for p in parts] == [2, 2, 2]


# ------------------------------- stitch.concat_clips -------------------------------

@pytest.mark.asyncio
async def test_concat_clips_crossfades_video_hardjoins_audio(monkeypatch):
    captured = {}

    def fake_probe(path, cfg):
        return (1080, 1920, 30.0, 15.0)
    def fake_normalize(src, dst, w, h, fps, cfg):
        Path(dst).write_bytes(b"N")                    # create the normalized file
    def fake_run(args, timeout):
        captured["args"] = args
        Path(args[-1]).write_bytes(b"STITCHED")        # ffmpeg writes the output (last arg)
    monkeypatch.setattr(stitch.broll, "_probe", fake_probe)
    monkeypatch.setattr(stitch.outro, "_normalize", fake_normalize)
    monkeypatch.setattr(stitch.broll, "_run_ffmpeg", fake_run)

    out = await stitch.concat_clips([b"a", b"b", b"c"])
    assert out == b"STITCHED"
    fc = " ".join(captured["args"])
    assert "xfade=transition=fade" in fc               # VIDEO is crossfaded
    assert fc.count("xfade") == 2                       # 3 clips → 2 joins
    assert "concat=n=3:v=0:a=1" in fc                   # AUDIO is hard-joined (no speech overlap)
    assert "atrim=end=14.600" in fc                     # non-final clips trimmed by the 0.4s xfade


@pytest.mark.asyncio
async def test_concat_clips_falls_back_to_hardcut_on_xfade_failure(monkeypatch):
    runs = []

    def fake_probe(path, cfg):
        return (1080, 1920, 30.0, 15.0)
    def fake_normalize(src, dst, w, h, fps, cfg):
        Path(dst).write_bytes(b"N")
    def fake_run(args, timeout):
        runs.append(" ".join(args))
        if len(runs) == 1:                              # first (crossfade) render fails
            raise stitch.broll.BrollError("xfade boom")
        Path(args[-1]).write_bytes(b"HARDCUT")
    monkeypatch.setattr(stitch.broll, "_probe", fake_probe)
    monkeypatch.setattr(stitch.outro, "_normalize", fake_normalize)
    monkeypatch.setattr(stitch.broll, "_run_ffmpeg", fake_run)

    out = await stitch.concat_clips([b"a", b"b", b"c"])
    assert out == b"HARDCUT"
    assert "xfade" in runs[0]                           # tried crossfade first
    assert "concat=n=3:v=1:a=1" in runs[1]              # then fell back to a hard-cut concat


@pytest.mark.asyncio
async def test_concat_clips_single_clip_skips_ffmpeg(monkeypatch):
    ran = {"n": 0}

    def fake_normalize(src, dst, w, h, fps, cfg):
        Path(dst).write_bytes(b"ONLY")
    def fake_run(args, timeout):
        ran["n"] += 1
    monkeypatch.setattr(stitch.broll, "_probe", lambda p, c: (1080, 1920, 30.0, 15.0))
    monkeypatch.setattr(stitch.outro, "_normalize", fake_normalize)
    monkeypatch.setattr(stitch.broll, "_run_ffmpeg", fake_run)

    out = await stitch.concat_clips([b"only"])
    assert out == b"ONLY"
    assert ran["n"] == 0                                # no concat ffmpeg for a single clip


@pytest.mark.asyncio
async def test_concat_clips_empty_raises():
    with pytest.raises(stitch.StitchError):
        await stitch.concat_clips([])


# ---- regression: normalize must pin duration with -t, never -shortest (the stitch-inflation bug) ----

def test_normalize_pins_exact_duration_not_shortest_with_audio(monkeypatch):
    from app.videos import outro, broll
    cap = {}
    monkeypatch.setattr(broll, "_probe", lambda src, cfg: (1080, 1920, 30.0, 15.0))
    monkeypatch.setattr(outro, "_has_audio", lambda src, cfg: True)
    monkeypatch.setattr(broll, "_run_ffmpeg", lambda args, t: cap.update(args=args))
    outro._normalize("in.mp4", "out.mp4", 1080, 1920, 30.0, broll.BrollConfig.from_settings())
    a = cap["args"]
    assert "-shortest" not in a                          # the bug: -shortest + apad inflated duration
    assert "-t" in a and a[a.index("-t") + 1] == "15.000"  # pinned to the source's exact duration
    assert "apad" in a


def test_normalize_pins_exact_duration_not_shortest_no_audio(monkeypatch):
    from app.videos import outro, broll
    cap = {}
    monkeypatch.setattr(broll, "_probe", lambda src, cfg: (1080, 1920, 30.0, 12.5))
    monkeypatch.setattr(outro, "_has_audio", lambda src, cfg: False)
    monkeypatch.setattr(broll, "_run_ffmpeg", lambda args, t: cap.update(args=args))
    outro._normalize("in.mp4", "out.mp4", 1080, 1920, 30.0, broll.BrollConfig.from_settings())
    a = cap["args"]
    assert "-shortest" not in a
    assert "-t" in a and a[a.index("-t") + 1] == "12.500"
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in a


# --------------------- poller: segment-state + stitch finalizer ---------------------

def test_cinematic_segments_state():
    done = [{"status": "completed", "video_url": "u1"}, {"status": "completed", "video_url": "u2"}]
    pending = [{"status": "completed", "video_url": "u1"}, {"status": "processing"}]
    no_url = [{"status": "completed", "video_url": "u1"}, {"status": "completed"}]  # url not ready
    failed = [{"status": "failed"}, {"status": "completed", "video_url": "u2"}]
    assert poller._cinematic_segments_state(done) == "done"
    assert poller._cinematic_segments_state(pending) == "pending"
    assert poller._cinematic_segments_state(no_url) == "pending"
    assert poller._cinematic_segments_state(failed) == "failed"


@pytest.mark.asyncio
async def test_merge_caption_finalize_stitches_then_delegates(monkeypatch):
    seen = {}

    async def fake_fetch(u):
        return b"clip:" + u.encode()
    async def fake_concat(clips, **_k):
        seen["clips"] = clips
        return b"MERGED"
    async def fake_upload(data, name, bucket):
        seen["uploaded"] = (data, name, bucket)
        return ("key", "https://s3/stitched.mp4")
    async def fake_finalize(video_id, raw_url, project_id, tg, script, add_outro, mode):
        seen["finalize"] = dict(raw_url=raw_url, add_outro=add_outro, mode=mode, script=script)
    async def fake_pname(_db, _pid):
        return "Proj"
    monkeypatch.setattr(poller.broll, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(poller.stitch, "concat_clips", fake_concat)
    monkeypatch.setattr(poller.storage, "upload_mp4", fake_upload)
    monkeypatch.setattr(poller, "_broll_caption_and_finalize", fake_finalize)
    monkeypatch.setattr(poller, "_project_name", fake_pname)

    await poller._merge_caption_and_finalize(
        uuid.uuid4(), ["https://a", "https://b", "https://c"], 1, None, "the full script")

    assert seen["clips"] == [b"clip:https://a", b"clip:https://b", b"clip:https://c"]
    assert seen["uploaded"][0] == b"MERGED"
    assert seen["finalize"]["raw_url"] == "https://s3/stitched.mp4"
    assert seen["finalize"]["add_outro"] is True        # outro ALWAYS appended
    assert seen["finalize"]["mode"] == "cinematic"      # so b-roll is skipped
    assert seen["finalize"]["script"] == "the full script"
