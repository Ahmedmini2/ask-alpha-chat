"""Unit tests for Supabase JWT auth on the chat API. The signature path is exercised end-to-end
with a locally-generated ES256 key (matching this project's asymmetric signing) and a stubbed
JWKS client; the live JWKS fetch is exercised in prod. resolve_user_id / _bearer / the dependency
policy are covered directly."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

import app.core.auth as auth


# --------------------------- ES256 verification (real crypto) ---------------------------

def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, priv.public_key()


def _token(priv, sub, *, aud="authenticated", exp_delta=3600, alg="ES256", extra=None):
    now = datetime.now(timezone.utc)
    claims = {"sub": str(sub), "aud": aud,
              "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=exp_delta)).timestamp())}
    if extra:
        claims.update(extra)
    return jwt.encode(claims, priv, algorithm=alg)


@pytest.fixture
def stub_jwks(monkeypatch):
    """Make auth verify against a key we control, with auth enabled."""
    priv, pub = _keypair()

    class _Key:
        key = pub

    class _Client:
        def get_signing_key_from_jwt(self, _token):
            return _Key()

    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")
    monkeypatch.setattr(auth, "_jwks_client", lambda: _Client())
    return priv


@pytest.mark.asyncio
async def test_verify_token_accepts_valid_token(stub_jwks):
    uid = uuid4()
    out = await auth.verify_token(_token(stub_jwks, uid))
    assert out == uid


@pytest.mark.asyncio
async def test_verify_token_rejects_expired(stub_jwks):
    with pytest.raises(jwt.ExpiredSignatureError):
        await auth.verify_token(_token(stub_jwks, uuid4(), exp_delta=-60))


@pytest.mark.asyncio
async def test_verify_token_rejects_wrong_audience(stub_jwks):
    with pytest.raises(jwt.InvalidAudienceError):
        await auth.verify_token(_token(stub_jwks, uuid4(), aud="anon"))


@pytest.mark.asyncio
async def test_verify_token_rejects_wrong_signer(stub_jwks):
    # token signed by a DIFFERENT key than the JWKS stub returns
    other_priv, _ = _keypair()
    with pytest.raises(jwt.InvalidSignatureError):
        await auth.verify_token(_token(other_priv, uuid4()))


@pytest.mark.asyncio
async def test_verify_token_rejects_missing_sub(stub_jwks):
    # craft a token with no sub — PyJWT's require=['sub'] must reject it
    priv = stub_jwks
    now = datetime.now(timezone.utc)
    tok = jwt.encode({"aud": "authenticated", "exp": int((now + timedelta(hours=1)).timestamp())},
                     priv, algorithm="ES256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        await auth.verify_token(tok)


# ------------------------------------- _bearer -------------------------------------

def test_bearer_none_when_absent():
    assert auth._bearer(None) is None
    assert auth._bearer("") is None


def test_bearer_extracts_token():
    assert auth._bearer("Bearer abc.def.ghi") == "abc.def.ghi"
    assert auth._bearer("bearer abc.def.ghi") == "abc.def.ghi"  # case-insensitive scheme


def test_bearer_rejects_malformed():
    for bad in ("abc.def.ghi", "Basic abc", "Bearer", "Bearer  ", "Bearer a b"):
        with pytest.raises(HTTPException) as ei:
            auth._bearer(bad)
        assert ei.value.status_code == 401


# ----------------------------------- resolve_user_id -----------------------------------

def test_resolve_disabled_trusts_body(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "")  # auth disabled
    body = uuid4()
    assert auth.resolve_user_id(None, body) == body          # legacy behavior
    assert auth.resolve_user_id(None, None) is None


def test_resolve_enabled_token_is_authoritative(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")
    uid = uuid4()
    assert auth.resolve_user_id(uid, None) == uid            # token, no body
    assert auth.resolve_user_id(uid, uid) == uid             # token == body


def test_resolve_enabled_no_token_is_anonymous_ignoring_body(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")
    # a spoofed body user_id with no token must NOT be trusted
    assert auth.resolve_user_id(None, uuid4()) is None


def test_resolve_enabled_mismatch_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")
    with pytest.raises(HTTPException) as ei:
        auth.resolve_user_id(uuid4(), uuid4())
    assert ei.value.status_code == 403


# ----------------------------------- authed_user_id dep -----------------------------------

@pytest.mark.asyncio
async def test_authed_user_id_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "")
    assert await auth.authed_user_id("Bearer whatever") is None


@pytest.mark.asyncio
async def test_authed_user_id_enabled_no_header_returns_none(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")
    assert await auth.authed_user_id(None) is None


@pytest.mark.asyncio
async def test_authed_user_id_enabled_valid_token(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")
    uid = uuid4()

    async def fake_verify(_t):
        return uid

    monkeypatch.setattr(auth, "verify_token", fake_verify)
    assert await auth.authed_user_id(f"Bearer good.token") == uid


@pytest.mark.asyncio
async def test_authed_user_id_enabled_bad_token_401(monkeypatch):
    monkeypatch.setattr(auth.settings, "supabase_url", "https://test.supabase.co")

    async def fake_verify(_t):
        raise jwt.InvalidTokenError("nope")

    monkeypatch.setattr(auth, "verify_token", fake_verify)
    with pytest.raises(HTTPException) as ei:
        await auth.authed_user_id("Bearer bad.token")
    assert ei.value.status_code == 401


# ------------------------- _assert_may_use_conversation (IDOR guard) -------------------------

import app.api.routes.chat as chat_route


class _Row:
    def __init__(self, user_id):
        self.id = uuid4()
        self.user_id = user_id


class _Res:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _DB:
    """Returns a canned single-row result. boom=True asserts the DB is never queried."""
    def __init__(self, row=None, boom=False):
        self._row = row
        self._boom = boom

    async def execute(self, *_a, **_k):
        if self._boom:
            raise AssertionError("DB should not be queried on this path")
        return _Res(self._row)


@pytest.mark.asyncio
async def test_idor_guard_noop_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "")
    # auth off → no DB query, never raises, regardless of conversation_id
    await chat_route._assert_may_use_conversation(_DB(boom=True), uuid4(), uuid4())


@pytest.mark.asyncio
async def test_idor_guard_noop_when_no_conversation_id(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "https://test.supabase.co")
    await chat_route._assert_may_use_conversation(_DB(boom=True), None, uuid4())


@pytest.mark.asyncio
async def test_idor_guard_unknown_conversation_is_allowed(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "https://test.supabase.co")
    # no such conversation → orchestrator mints a fresh one; must not raise
    await chat_route._assert_may_use_conversation(_DB(row=None), uuid4(), uuid4())


@pytest.mark.asyncio
async def test_idor_guard_owner_may_continue(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "https://test.supabase.co")
    me = uuid4()
    await chat_route._assert_may_use_conversation(_DB(row=_Row(me)), uuid4(), me)


@pytest.mark.asyncio
async def test_idor_guard_blocks_other_users_conversation(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "https://test.supabase.co")
    victim, attacker = uuid4(), uuid4()
    with pytest.raises(HTTPException) as ei:
        await chat_route._assert_may_use_conversation(_DB(row=_Row(victim)), uuid4(), attacker)
    assert ei.value.status_code == 404  # 404 (not 403) so existence isn't leaked


@pytest.mark.asyncio
async def test_idor_guard_blocks_anonymous_from_owned_conversation(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "https://test.supabase.co")
    victim = uuid4()
    with pytest.raises(HTTPException) as ei:
        await chat_route._assert_may_use_conversation(_DB(row=_Row(victim)), uuid4(), None)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_idor_guard_anonymous_conversation_is_bearer(monkeypatch):
    monkeypatch.setattr(chat_route.settings, "supabase_url", "https://test.supabase.co")
    # an unowned (user_id IS NULL) conversation stays continuable — preserves anonymous chat
    await chat_route._assert_may_use_conversation(_DB(row=_Row(None)), uuid4(), None)
    await chat_route._assert_may_use_conversation(_DB(row=_Row(None)), uuid4(), uuid4())
