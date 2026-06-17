"""Supabase access-token verification for the chat API.

The web app's /api/chat used to inject the authenticated user_id into the request BODY, and the
backend trusted it. With no authentication on /v1/chat, anyone who knew (or guessed) a user's
Supabase auth id could act as that user — and now that publish_to_social posts to real social
accounts, that is a cross-tenant takeover-of-action. This module closes that gap by deriving the
identity from a verified Supabase JWT instead of the body.

This Supabase project signs tokens with ASYMMETRIC keys (ES256), so we verify against the
project's PUBLIC JWKS — no shared secret is stored anywhere. PyJWKClient caches the keys.

Policy (see resolve_user_id):
- settings.supabase_url SET  -> auth ENFORCED. A valid Bearer token is authoritative; its `sub`
  is the user_id. A missing token => anonymous (the body user_id is ignored). A token that
  disagrees with a body user_id => 403. An invalid/expired token => 401.
- settings.supabase_url UNSET -> auth DISABLED (dev / pre-rollout): the legacy body user_id is
  trusted, with a warning. Set SUPABASE_URL in prod to turn enforcement on.

Zero-downtime rollout: (1) update the web app to forward the user's access token as
`Authorization: Bearer <token>` (harmless while SUPABASE_URL is unset — body is still trusted),
then (2) set SUPABASE_URL on the backend → the token becomes authoritative and the body is no
longer trusted, with no window where authenticated features break.
"""
import asyncio
import logging
from typing import Optional
from uuid import UUID

import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

from app.config import settings

log = logging.getLogger("askalpha.auth")

# Asymmetric algorithms only — NEVER include HS256 here while verifying with a public key, or a
# forged HS256 token signed with the public key as the HMAC secret would pass (alg-confusion).
_ALGORITHMS = ["ES256", "RS256"]
_AUDIENCE = "authenticated"   # Supabase access tokens for signed-in users carry aud=authenticated
_LEEWAY = 10                  # seconds of clock-skew tolerance on exp/iat

_jwk_client: Optional[PyJWKClient] = None


def auth_enabled() -> bool:
    return bool(settings.supabase_url)


def _jwks_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        # cache_keys keeps the fetched public keys in-process; lifespan bounds staleness so a key
        # rotation is picked up without a restart.
        _jwk_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=600)
    return _jwk_client


def _verify_sync(token: str) -> dict:
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=_ALGORITHMS,
        audience=_AUDIENCE,
        leeway=_LEEWAY,
        options={"require": ["exp", "sub"]},
    )


async def verify_token(token: str) -> UUID:
    """Verify a Supabase access token and return its user id (the `sub` claim). Raises on any
    failure (bad signature, expired, wrong audience, malformed sub). PyJWKClient may do a blocking
    HTTP fetch on a cache miss, so run it off the event loop."""
    payload = await asyncio.to_thread(_verify_sync, token)
    return UUID(str(payload["sub"]))


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an 'Authorization: Bearer <token>' header, or None if absent.
    Raises 401 on a malformed header (present but not a well-formed Bearer)."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(status_code=401, detail="Malformed Authorization header")
    return parts[1]


async def authed_user_id(authorization: Optional[str] = Header(default=None)) -> Optional[UUID]:
    """FastAPI dependency. Returns the verified user id from the Bearer token, or None if no token
    was sent. Raises 401 on a malformed/invalid/expired token. Returns None when auth is disabled
    (the route then falls back to the legacy body user_id via resolve_user_id)."""
    if not auth_enabled():
        return None
    token = _bearer(authorization)
    if token is None:
        return None
    try:
        return await verify_token(token)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("JWT verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def resolve_user_id(authed: Optional[UUID], body_user_id: Optional[UUID]) -> Optional[UUID]:
    """The effective, trustworthy user_id for a request.

    - Auth disabled: trust the body user_id (legacy behavior), warn once it's relied on.
    - Auth enabled: the verified token is authoritative. No token => anonymous (body ignored).
      A body user_id that disagrees with the token => 403 (likely a spoof attempt or a stale id).
    """
    if not auth_enabled():
        if body_user_id is not None:
            log.warning("auth disabled (SUPABASE_URL unset) — trusting body user_id %s", body_user_id)
        return body_user_id
    if authed is None:
        return None
    if body_user_id is not None and body_user_id != authed:
        raise HTTPException(status_code=403, detail="user_id does not match the authenticated user")
    return authed
