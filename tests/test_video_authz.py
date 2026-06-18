"""Authorization tests for promo-video avatar resolution — the rule that a user can ONLY ever
generate a video with their OWN avatar (their connected heygen_avatars row, or a HeyGen group in
their own name), never another person's. Covers the pure name helpers, the _resolve_self_avatar
priority/erroring, and the handler-boundary rejection of a cross-person agent_name."""
import uuid
import pytest

import app.tools.videos as videos
from app.tools.videos import (
    _agent_name_targets_other,
    _email_local,
    _resolve_self_avatar,
    _resolve_voice,
    _self_display_name,
    _self_identity_tokens,
    create_promo_video_handler,
    list_avatar_looks_handler,
)


class FakeProfile:
    def __init__(self, first_name=None, last_name=None, email=None,
                 role="salesagent", ask_alpha_access="write"):
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.role = role
        self.ask_alpha_access = ask_alpha_access


class FakeAvatar:
    def __init__(self, group_id="g1", avatar_id="av1", name="ahmed.othman",
                 status="completed", consent_status="accepted",
                 preview_image_url="https://x/p.png", error_message=None):
        self.group_id = group_id
        self.avatar_id = avatar_id
        self.name = name
        self.status = status
        self.consent_status = consent_status
        self.preview_image_url = preview_image_url
        self.error_message = error_message


# --------------------------------- pure name helpers ---------------------------------

def test_email_local_splits_separators():
    assert _email_local(FakeProfile(email="ahmed.othman@allegiance.ae")) == "ahmed othman"
    assert _email_local(FakeProfile(email="zain_ul-abdeen@x.com")) == "zain ul abdeen"
    assert _email_local(FakeProfile(email=None)) == ""


def test_self_display_name_prefers_full_name_then_avatar_then_email():
    p = FakeProfile(first_name="Zain", last_name="Ul Abdeen", email="z@x.com")
    assert _self_display_name(p, None) == "Zain Ul Abdeen"
    # no profile name -> connected avatar name
    p2 = FakeProfile(email="ahmed.othman@allegiance.ae")
    assert _self_display_name(p2, FakeAvatar(name="ahmed.othman")) == "ahmed.othman"
    # no name + no avatar -> email local part
    assert _self_display_name(p2, None) == "ahmed othman"


def test_agent_name_targets_other_blocks_strangers_allows_self():
    me = FakeProfile(first_name="Zain", last_name="Ul Abdeen", email="zain@x.com")
    av = FakeAvatar(name="zain")
    # empty / self → not "other"
    assert _agent_name_targets_other("", me, av) is False
    assert _agent_name_targets_other("Zain Ul Abdeen", me, av) is False
    assert _agent_name_targets_other("Zain", me, av) is False           # shared first token
    # clearly someone else → rejected
    assert _agent_name_targets_other("Chinoy", me, av) is True
    assert _agent_name_targets_other("Ahmed", me, av) is True
    assert _agent_name_targets_other("Rami Nabil", me, av) is True


def test_agent_name_targets_other_matches_email_local_for_nameless_profile():
    # ahmed has no first/last name; his identity comes from the avatar/email
    me = FakeProfile(email="ahmed.othman@allegiance.ae")
    av = FakeAvatar(name="ahmed.othman")
    assert _agent_name_targets_other("Ahmed Othman", me, av) is False   # matches email local
    assert _agent_name_targets_other("ahmed.othman", me, av) is False
    assert _agent_name_targets_other("Zain", me, av) is True


def test_self_identity_tokens_collects_all_aliases():
    me = FakeProfile(first_name="Zain", last_name="Ul Abdeen", email="zain.abdeen@x.com")
    toks = _self_identity_tokens(me, FakeAvatar(name="zain"))
    assert "zain ul abdeen" in toks
    assert "zain abdeen" in toks   # email local
    assert "" not in toks


# ------------------------------- _resolve_self_avatar -------------------------------

def _patch_resolver(monkeypatch, *, av, connected_looks=None, name_looks=None):
    async def fake_get_avatar(_db, _uid):
        return av
    async def fake_connected(group_id, avatar_id, preview, person):
        return list(connected_looks or [])
    async def fake_list_looks_for_self(identity_names, person):
        return list(name_looks or [])
    monkeypatch.setattr(videos, "get_heygen_avatar", fake_get_avatar)
    monkeypatch.setattr(videos.heygen, "looks_for_connected_avatar", fake_connected)
    monkeypatch.setattr(videos.heygen, "list_looks_for_self", fake_list_looks_for_self)


@pytest.mark.asyncio
async def test_resolve_prefers_connected_avatar(monkeypatch):
    looks = [{"look_name": "Original", "avatar_id": "av1", "is_photo": True}]
    _patch_resolver(monkeypatch, av=FakeAvatar(), connected_looks=looks,
                    name_looks=[{"look_name": "SHOULD-NOT-USE", "avatar_id": "x"}])
    out, err, meta = await _resolve_self_avatar(object(), FakeProfile(email="ahmed.othman@x.com"), uuid.uuid4())
    assert err is None
    assert out == looks
    assert meta["source"] == "connected"


@pytest.mark.asyncio
async def test_resolve_errors_when_connected_avatar_not_ready(monkeypatch):
    _patch_resolver(monkeypatch, av=FakeAvatar(status="processing"), connected_looks=[{"x": 1}])
    out, err, _ = await _resolve_self_avatar(object(), FakeProfile(), uuid.uuid4())
    assert out is None
    assert "still being created" in err["error"]


@pytest.mark.asyncio
async def test_resolve_errors_when_consent_pending(monkeypatch):
    _patch_resolver(monkeypatch, av=FakeAvatar(consent_status="pending"), connected_looks=[{"x": 1}])
    out, err, _ = await _resolve_self_avatar(object(), FakeProfile(), uuid.uuid4())
    assert out is None
    assert "consent" in err["error"].lower()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_name_when_no_connected_row(monkeypatch):
    name_looks = [{"look_name": "Original", "avatar_id": "z1"}]
    _patch_resolver(monkeypatch, av=None, name_looks=name_looks)
    me = FakeProfile(first_name="Zain", last_name="Ul Abdeen")
    out, err, meta = await _resolve_self_avatar(object(), me, uuid.uuid4())
    assert err is None
    assert out == name_looks
    assert meta["source"] == "name-match"
    assert meta["display_name"] == "Zain Ul Abdeen"


@pytest.mark.asyncio
async def test_resolve_errors_when_no_avatar_anywhere(monkeypatch):
    _patch_resolver(monkeypatch, av=None, name_looks=[])
    me = FakeProfile(first_name="Newbie", last_name="Agent")
    out, err, _ = await _resolve_self_avatar(object(), me, uuid.uuid4())
    assert out is None
    assert "No AI avatar found" in err["error"]


@pytest.mark.asyncio
async def test_resolve_errors_when_no_name_and_no_row(monkeypatch):
    _patch_resolver(monkeypatch, av=None, name_looks=[])
    out, err, _ = await _resolve_self_avatar(object(), FakeProfile(email=None), uuid.uuid4())
    assert out is None
    assert "No AI avatar is connected" in err["error"]


@pytest.mark.asyncio
async def test_resolve_reuses_prefetched_av_without_requerying(monkeypatch):
    # When the handler passes av=, the resolver must NOT issue a second get_heygen_avatar query.
    async def boom_get_avatar(_db, _uid):
        raise AssertionError("get_heygen_avatar must not be called when av is provided")
    async def fake_connected(group_id, avatar_id, preview, person):
        return [{"look_name": "Original", "avatar_id": avatar_id}]
    monkeypatch.setattr(videos, "get_heygen_avatar", boom_get_avatar)
    monkeypatch.setattr(videos.heygen, "looks_for_connected_avatar", fake_connected)
    out, err, meta = await _resolve_self_avatar(
        object(), FakeProfile(email="ahmed.othman@x.com"), uuid.uuid4(), av=FakeAvatar())
    assert err is None
    assert meta["source"] == "connected"
    assert out[0]["avatar_id"] == "av1"


@pytest.mark.asyncio
async def test_resolve_falls_back_for_nameless_user_via_email_identity(monkeypatch):
    # No connected row, no first/last name, but the email gives an identity ("ahmed othman").
    captured = {}
    async def fake_get_avatar(_db, _uid):
        return None
    async def fake_self(identity_names, person):
        captured["identity"] = set(identity_names)
        captured["person"] = person
        return [{"look_name": "Original", "avatar_id": "x"}]
    monkeypatch.setattr(videos, "get_heygen_avatar", fake_get_avatar)
    monkeypatch.setattr(videos.heygen, "list_looks_for_self", fake_self)
    out, err, meta = await _resolve_self_avatar(
        object(), FakeProfile(email="ahmed.othman@allegiance.ae"), uuid.uuid4())
    assert err is None and meta["source"] == "name-match"
    assert "ahmed othman" in captured["identity"]   # identity derived from the caller's own email


# --------------------- _resolve_voice no longer first-token-matches a stranger ---------------------

@pytest.mark.asyncio
async def test_resolve_voice_never_searches_first_name_token(monkeypatch):
    # The voice search must use the EXACT full name only — never the bare first token, which could
    # bind a same-first-name stranger's voice. We record every name find_voice_by_name is queried
    # with and assert the lone first token ("Ahmed") is never among them.
    monkeypatch.setattr(videos.settings, "heygen_agent_voices", "{}")  # neutralize any env pin
    seen = []
    async def fake_find_voice(name):
        seen.append(name)
        return None
    async def fake_list_voices():
        return []
    monkeypatch.setattr(videos.heygen, "find_voice_by_name", fake_find_voice)
    monkeypatch.setattr(videos.heygen, "list_voices", fake_list_voices)
    voice, source, _ = await _resolve_voice("Ahmed Othman", {"default_voice_id": None})
    assert voice is None and source == "none"
    assert "Ahmed" not in seen          # never the bare first token
    assert seen == ["Ahmed Othman"]     # only the full name was queried


# --------------------------- handler-boundary cross-person reject ---------------------------

@pytest.fixture
def _agent(monkeypatch):
    async def fake_get_profile(_db, _uid):
        return FakeProfile(first_name="Zain", last_name="Ul Abdeen", email="zain@x.com")
    async def fake_get_avatar(_db, _uid):
        return FakeAvatar(name="zain")
    monkeypatch.setattr(videos, "get_profile", fake_get_profile)
    monkeypatch.setattr(videos, "is_agent", lambda p: True)
    monkeypatch.setattr(videos, "get_heygen_avatar", fake_get_avatar)


@pytest.mark.asyncio
async def test_create_promo_rejects_video_for_another_person(_agent):
    ctx = {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}
    out = await create_promo_video_handler(object(), {"agent_name": "Chinoy",
                                                      "project_name": "Anything"}, ctx)
    assert "error" in out
    assert "OWN AI avatar" in out["error"]
    assert "Chinoy" in out["error"]


@pytest.mark.asyncio
async def test_list_looks_rejects_another_person(_agent):
    ctx = {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}
    out = await list_avatar_looks_handler(object(), {"agent_name": "Ahmed Othman"}, ctx)
    assert "error" in out
    assert "your OWN AI avatar" in out["error"]


@pytest.mark.asyncio
async def test_create_promo_allows_own_first_name(monkeypatch, _agent):
    # agent_name that IS the caller (their own first name) must pass the guard — it then proceeds
    # to project resolution. We stub _resolve_project so reaching it proves the guard let "Zain"
    # (self) through; the returned error is the project stub's, NOT the authorization one.
    async def fake_resolve_project(_db, _args):
        return None, {"error": "PROJECT-STUB-REACHED"}
    monkeypatch.setattr(videos, "_resolve_project", fake_resolve_project)
    ctx = {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}
    out = await create_promo_video_handler(object(), {"agent_name": "Zain",
                                                      "project_name": "Whatever"}, ctx)
    assert out.get("error") == "PROJECT-STUB-REACHED"   # guard passed → reached project step
    assert "OWN AI avatar" not in out.get("error", "")
