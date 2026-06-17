"""Unit tests for the v2 social-agent tools: the approval gate (auto/ask/deny + confirmed),
the permission read's fail-safe default, platform normalization, and the read/action handler
branches (connection gate, gate short-circuit, success). Ayrshare HTTP is monkeypatched; the
live calls are exercised in prod, like the other network paths in this suite."""
import uuid

import pytest

import app.tools.social as social
from app.tools.social import (
    _norm_platform,
    get_social_permission,
    publish_to_social_handler,
    list_posts_handler,
    get_messages_handler,
    reply_to_comment_handler,
    send_dm_handler,
)


# --------------------------------- _norm_platform ---------------------------------

def test_norm_platform_aliases_and_case():
    assert _norm_platform("X") == "twitter"
    assert _norm_platform("Instagram") == "instagram"
    assert _norm_platform("twitter.com") == "twitter"
    assert _norm_platform("") is None
    assert _norm_platform(None) is None


# --------------------------- get_social_permission (fail-safe) ---------------------------

class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Returns canned results in order. boom=True asserts the DB is never queried; raises=True
    makes execute() throw (to exercise the fail-safe)."""
    def __init__(self, results=(), boom=False, raises=False):
        self._results = list(results)
        self._boom = boom
        self._raises = raises

    async def execute(self, *_a, **_k):
        if self._boom:
            raise AssertionError("DB should not be queried on this path")
        if self._raises:
            raise RuntimeError("db down")
        return _Result(self._results.pop(0))


def _ctx():
    return {"user_id": uuid.uuid4(), "channel": "website", "conversation_id": uuid.uuid4()}


@pytest.mark.asyncio
async def test_permission_reads_value():
    assert await get_social_permission(FakeSession(results=["auto"]), uuid.uuid4()) == "auto"
    assert await get_social_permission(FakeSession(results=["deny"]), uuid.uuid4()) == "deny"


@pytest.mark.asyncio
async def test_permission_defaults_ask_on_missing_row():
    assert await get_social_permission(FakeSession(results=[None]), uuid.uuid4()) == "ask"


@pytest.mark.asyncio
async def test_permission_defaults_ask_on_bad_value_or_error_or_anon():
    assert await get_social_permission(FakeSession(results=["bogus"]), uuid.uuid4()) == "ask"
    assert await get_social_permission(FakeSession(raises=True), uuid.uuid4()) == "ask"
    assert await get_social_permission(FakeSession(boom=True), None) == "ask"  # anon never hits DB


# ----------------------------------- the approval gate -----------------------------------
# Drive the gate through publish_to_social_handler. Patch get_social_permission to the mode and
# get_profile_key so we don't depend on DB rows; assert side-effect vs short-circuit.

@pytest.fixture
def patch_pk(monkeypatch):
    async def fake_pk(_db, _uid):
        return "pk_123"
    monkeypatch.setattr(social, "get_profile_key", fake_pk)


def _force_permission(monkeypatch, mode):
    async def fake_perm(_db, _uid):
        return mode
    monkeypatch.setattr(social, "get_social_permission", fake_perm)


@pytest.mark.asyncio
async def test_deny_blocks_publish_without_executing(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "deny")

    async def boom(*a, **k):
        raise AssertionError("must not publish under deny")

    monkeypatch.setattr(social.ayrshare, "publish", boom)
    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", boom)
    out = await publish_to_social_handler(
        FakeSession(), {"platforms": ["facebook"], "caption": "hi"}, _ctx())
    assert out["status"] == "denied"


@pytest.mark.asyncio
async def test_ask_without_confirm_is_pending_no_publish(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "ask")

    async def boom(*a, **k):
        raise AssertionError("must not publish before confirmation")

    monkeypatch.setattr(social.ayrshare, "publish", boom)
    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", boom)
    out = await publish_to_social_handler(
        FakeSession(), {"platforms": ["facebook"], "caption": "Big news"}, _ctx())
    assert out["status"] == "pending_confirmation"
    assert out["draft"]["caption"] == "Big news"
    assert out["draft"]["platforms"] == ["facebook"]


@pytest.mark.asyncio
async def test_ask_with_confirmed_executes(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "ask")

    async def fake_linked(_pk):
        return ["facebook"]

    async def fake_publish(*a, **k):
        return {"status": "success", "postIds": [{"postUrl": "https://fb/p/1"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(), {"platforms": ["facebook"], "caption": "Big news", "confirmed": True}, _ctx())
    assert out["status"] == "published"
    assert out["post_urls"] == ["https://fb/p/1"]


@pytest.mark.asyncio
async def test_auto_executes_without_confirm(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "auto")

    async def fake_linked(_pk):
        return ["facebook"]

    async def fake_publish(*a, **k):
        return {"status": "success", "postIds": [{"postUrl": "https://fb/p/2"}]}

    monkeypatch.setattr(social.ayrshare, "get_linked_platforms", fake_linked)
    monkeypatch.setattr(social.ayrshare, "publish", fake_publish)
    out = await publish_to_social_handler(
        FakeSession(), {"platforms": ["facebook"], "caption": "auto post"}, _ctx())
    assert out["status"] == "published"


@pytest.mark.asyncio
async def test_invalid_input_short_circuits_before_gate(monkeypatch):
    # no platform: rejected before any permission read or DB access
    out = await publish_to_social_handler(
        FakeSession(boom=True), {"caption": "hi"}, _ctx())
    assert out["status"] == "needs_input"


# ----------------------------------- read tools -----------------------------------

@pytest.mark.asyncio
async def test_list_posts_returns_capped_posts(monkeypatch, patch_pk):
    posts = [{"id": str(i), "post": f"p{i}"} for i in range(50)]

    async def fake_history(_pk, _plat):
        return posts

    monkeypatch.setattr(social.ayrshare, "get_post_history", fake_history)
    out = await list_posts_handler(FakeSession(), {"platform": "instagram"}, _ctx())
    assert out["status"] == "ok"
    assert out["count"] == 50
    assert len(out["posts"]) == social._READ_CAP  # capped


@pytest.mark.asyncio
async def test_read_tool_requires_connection(monkeypatch):
    async def no_pk(_db, _uid):
        return None

    monkeypatch.setattr(social, "get_profile_key", no_pk)
    out = await list_posts_handler(FakeSession(), {"platform": "instagram"}, _ctx())
    assert out["status"] == "not_connected"


@pytest.mark.asyncio
async def test_get_messages_rejects_unsupported_platform(monkeypatch):
    # linkedin DMs aren't supported — rejected before connection/DB
    out = await get_messages_handler(FakeSession(boom=True), {"platform": "linkedin"}, _ctx())
    assert out["status"] == "needs_input"


@pytest.mark.asyncio
async def test_get_messages_flags_ids_not_usernames(monkeypatch, patch_pk):
    async def fake_msgs(_pk, _plat, _conv):
        return [{"senderId": "123", "message": "hi"}]

    monkeypatch.setattr(social.ayrshare, "get_messages", fake_msgs)
    out = await get_messages_handler(FakeSession(), {"platform": "instagram"}, _ctx())
    assert out["status"] == "ok"
    assert "IDs" in out["note"]


# ----------------------------------- action tools (gate) -----------------------------------

@pytest.mark.asyncio
async def test_reply_to_comment_pending_then_executes(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "ask")
    sent = {}

    async def fake_reply(_pk, cid, plat, reply):
        sent["args"] = (cid, plat, reply)
        return {"status": "success"}

    monkeypatch.setattr(social.ayrshare, "reply_to_comment", fake_reply)

    args = {"comment_id": "c1", "platform": "instagram", "reply": "thank you!"}
    pending = await reply_to_comment_handler(FakeSession(), args, _ctx())
    assert pending["status"] == "pending_confirmation"
    assert "args" not in sent  # nothing sent yet

    out = await reply_to_comment_handler(FakeSession(), {**args, "confirmed": True}, _ctx())
    assert out["status"] == "replied"
    assert sent["args"] == ("c1", "instagram", "thank you!")


@pytest.mark.asyncio
async def test_send_dm_deny(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "deny")

    async def boom(*a, **k):
        raise AssertionError("must not send under deny")

    monkeypatch.setattr(social.ayrshare, "send_dm", boom)
    out = await send_dm_handler(
        FakeSession(), {"platform": "instagram", "recipient_id": "42", "message": "hello"}, _ctx())
    assert out["status"] == "denied"


@pytest.mark.asyncio
async def test_send_dm_auto_executes(monkeypatch, patch_pk):
    _force_permission(monkeypatch, "auto")
    sent = {}

    async def fake_dm(_pk, plat, rid, msg, media_urls=None):
        sent["args"] = (plat, rid, msg, media_urls)
        return {"status": "success"}

    monkeypatch.setattr(social.ayrshare, "send_dm", fake_dm)
    out = await send_dm_handler(
        FakeSession(), {"platform": "instagram", "recipient_id": "42", "message": "hi there"}, _ctx())
    assert out["status"] == "sent"
    assert sent["args"] == ("instagram", "42", "hi there", None)


@pytest.mark.asyncio
async def test_send_dm_rejects_non_https_media(monkeypatch):
    out = await send_dm_handler(
        FakeSession(boom=True),
        {"platform": "instagram", "recipient_id": "42", "message": "hi", "media_url": "http://x/y.jpg"},
        _ctx())
    assert out["status"] == "needs_input"
