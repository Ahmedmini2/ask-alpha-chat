import re
from typing import Optional
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Profile, HeygenAvatar

AGENT_ROLES = {"salesagent", "admin"}
ACCESS_GRANTED = {"read", "write"}

# A heygen_avatars row is only generation-ready once HeyGen has finished training the twin.
# We block on the states that are clearly NOT ready (everything else — including 'completed',
# 'ready', or an absent status on an otherwise-populated row — is treated as usable).
AVATAR_NOT_READY = {"pending", "processing", "queued", "training", "in_progress",
                    "failed", "error", "deleted"}
# Consent the user signs for their own likeness. HeyGen also enforces this at render time; we
# pre-check only to give a friendlier message. Block on the clearly-negative states.
CONSENT_NOT_OK = {"pending", "rejected", "declined", "revoked", "expired", "required"}


def _normalize_phone(phone: str) -> str:
    """Strip everything but digits and a leading +."""
    s = phone.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    return ("+" + digits) if plus else digits


async def get_profile(db: AsyncSession, user_id: UUID) -> Optional[Profile]:
    return (await db.execute(select(Profile).where(Profile.id == user_id))).scalar_one_or_none()


async def get_profile_by_phone(db: AsyncSession, phone: str) -> Optional[Profile]:
    """Match by normalized phone; tries the stored value and the normalized form."""
    norm = _normalize_phone(phone)
    no_plus = norm.lstrip("+")

    # Try exact, then variants. Phone numbers in profiles can be stored inconsistently.
    rows = (await db.execute(
        select(Profile).where(
            (Profile.phone == phone) |
            (Profile.phone == norm) |
            (Profile.phone == no_plus) |
            (Profile.phone == "+" + no_plus)
        )
    )).scalars().all()
    return rows[0] if rows else None


def is_agent(profile: Optional[Profile]) -> bool:
    """Agents are profiles with an agent role AND any ask-alpha access."""
    if profile is None:
        return False
    if profile.role not in AGENT_ROLES:
        return False
    if profile.ask_alpha_access not in ACCESS_GRANTED:
        return False
    return True


async def get_heygen_avatar(db: AsyncSession, user_id: UUID) -> Optional[HeygenAvatar]:
    """The caller's OWN connected HeyGen avatar (digital twin), keyed by user_id. This is the
    authoritative person→avatar link: a present row means the avatar is theirs and theirs alone."""
    return (await db.execute(
        select(HeygenAvatar).where(HeygenAvatar.user_id == user_id)
    )).scalar_one_or_none()


def heygen_avatar_status_error(av: HeygenAvatar) -> Optional[str]:
    """If the connected avatar exists but can't be used yet, return a user-facing reason; else None.
    Callers use this to fail with a clear message instead of silently falling back to name-matching
    (which could resolve a *different* person's avatar)."""
    status = (av.status or "").strip().lower()
    if status in AVATAR_NOT_READY:
        if status in ("failed", "error"):
            extra = f" ({av.error_message})" if av.error_message else ""
            return ("Your AI avatar failed to finish training in HeyGen" + extra +
                    ". Please re-record it in Alpha Chat → Settings.")
        return ("Your AI avatar is still being created (status: "
                f"{status or 'processing'}). Try again once it's ready.")
    if not (av.group_id or av.avatar_id):
        return ("Your AI avatar record is incomplete — no HeyGen avatar id yet. "
                "Please re-record it in Alpha Chat → Settings.")
    consent = (av.consent_status or "").strip().lower()
    if consent in CONSENT_NOT_OK:
        return ("Your AI-avatar consent isn't completed yet. Please finish the consent step in "
                "Alpha Chat → Settings before generating a video.")
    return None
