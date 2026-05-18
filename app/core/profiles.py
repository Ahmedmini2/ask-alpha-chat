import re
from typing import Optional
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Profile

AGENT_ROLES = {"salesagent", "admin"}
ACCESS_GRANTED = {"read", "write"}


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
