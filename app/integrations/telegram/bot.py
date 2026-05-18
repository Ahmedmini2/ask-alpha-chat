"""Telegram polling bot. Verifies users by phone-share, then forwards chat
messages through the main orchestrator scoped to that profile."""
import asyncio
import logging
import re
from typing import Optional
from uuid import UUID

# Permissive: starts with optional +, then digits and common separators, 7+ chars total.
PHONE_RE = re.compile(r"^\+?[\d\s\-().]{7,}$")

from sqlalchemy import select, text
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.core.orchestrator import chat_turn
from app.core.profiles import get_profile, get_profile_by_phone, is_agent
from app.db.models import MessagingLink
from app.db.session import AsyncSessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("askalpha.telegram")

# Track current conversation per Telegram chat so multi-turn works.
# Lost on bot restart; that's fine for an MVP.
CHAT_CONV: dict[int, UUID] = {}


async def _get_linked_profile_id(db, chat_id: int) -> Optional[UUID]:
    row = (await db.execute(
        select(MessagingLink).where(
            MessagingLink.channel == "telegram",
            MessagingLink.external_id == str(chat_id),
        )
    )).scalar_one_or_none()
    return row.profile_id if row else None


async def _upsert_link(db, chat_id: int, profile_id: UUID) -> None:
    await db.execute(
        text("""
            INSERT INTO messaging_links (profile_id, channel, external_id, created_at)
            VALUES (:pid, 'telegram', :eid, NOW())
            ON CONFLICT (channel, external_id)
            DO UPDATE SET profile_id = EXCLUDED.profile_id
        """),
        {"pid": str(profile_id), "eid": str(chat_id)},
    )
    await db.commit()


def _format_cards(cards: list[dict]) -> str:
    """Convert cards into a plaintext block suitable for Telegram replies."""
    lines: list[str] = []
    for c in cards:
        kind = c.get("type")
        if kind == "project_list":
            items = c.get("items", [])
            if not items:
                continue
            lines.append(f"\n📋 *Projects* ({len(items)} shown" +
                         (", more available" if c.get("has_more") else "") + "):")
            for p in items:
                price = ""
                if p.get("min_price") and p.get("max_price"):
                    price = f" — {p.get('currency') or ''} {int(p['min_price']):,}–{int(p['max_price']):,}"
                loc = p.get("city") or p.get("region") or ""
                dev = p.get("developer") or ""
                lines.append(f"  • *{p.get('name')}* ({dev}, {loc}){price}")
        elif kind == "document_quotes":
            items = c.get("items", [])
            if items:
                lines.append(f"\n📄 _Found {len(items)} brochure excerpts._")
        elif kind == "video_job":
            lines.append(
                f"\n🎬 *Video job started* — id `{c.get('video_id')}` "
                f"(status: {c.get('status')}). It usually takes 1–2 minutes. "
                f"Ask _\"is my video ready?\"_ to check."
            )
        elif kind == "video_status":
            status = c.get("status")
            if status == "completed" and c.get("video_url"):
                lines.append(
                    f"\n✅ *Your video is ready!*\n"
                    f"Download / share: {c.get('video_url')}"
                )
            elif status == "failed":
                detail = c.get("error_detail") or "unknown"
                lines.append(f"\n❌ *Video failed:* {detail}")
            else:
                lines.append(
                    f"\n⏳ Still rendering (status: {status}). Try again in a minute."
                )
    return "\n".join(lines).strip()


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as db:
        pid = await _get_linked_profile_id(db, chat_id)
        if pid is not None:
            profile = await get_profile(db, pid)
            if is_agent(profile):
                name = profile.first_name or "agent"
                await update.message.reply_text(
                    f"Welcome back, {name}. Ask me anything about projects, brochures, or to create a promo video."
                )
                return

    button = KeyboardButton("📱 Share my phone to verify", request_contact=True)
    markup = ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text(
        "Welcome to Ask Alpha 🏗️\n"
        "Tap the *📱 Share my phone to verify* button below — that's the secure way.\n"
        "Or, just type your phone number (e.g. `+971585217566`) and I'll try to match it.",
        reply_markup=markup,
        parse_mode="Markdown",
    )


async def on_contact(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    chat_id = update.effective_chat.id
    phone = contact.phone_number

    # Ensure the user shared their OWN contact, not someone else's.
    if contact.user_id and contact.user_id != update.effective_user.id:
        await update.message.reply_text("Please share *your own* contact, not another user's.",
                                        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        return

    async with AsyncSessionLocal() as db:
        profile = await get_profile_by_phone(db, phone)
        if profile is None:
            await update.message.reply_text(
                "I couldn't find an agent profile linked to that phone number. "
                "Please contact your admin to be added.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if not is_agent(profile):
            await update.message.reply_text(
                "Your profile was found but doesn't have agent access. Contact your admin.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        await _upsert_link(db, chat_id, profile.id)

    name = profile.first_name or "agent"
    await update.message.reply_text(
        f"Verified ✅ Welcome {name}.\n\nAsk me anything — for example:\n"
        "  • _\"Show me damac projects\"_\n"
        "  • _\"What does the Catch Residence brochure say about amenities?\"_\n"
        "  • _\"Make a promo video for project 6\"_",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )


async def _try_verify_by_typed_phone(update: Update, db, chat_id: int, text: str) -> bool:
    """If the text looks like a phone, try to match a profile. Returns True if handled."""
    stripped = text.strip()
    if not PHONE_RE.match(stripped):
        return False
    profile = await get_profile_by_phone(db, stripped)
    if profile is None:
        await update.message.reply_text(
            "I couldn't find any profile with that phone. Double-check the number, or "
            "tap *📱 Share my phone to verify* below to send your real Telegram-verified number.",
            parse_mode="Markdown",
        )
        return True
    if not is_agent(profile):
        await update.message.reply_text(
            "I found a profile for that phone, but it doesn't have agent access. "
            "Contact your admin."
        )
        return True
    await _upsert_link(db, chat_id, profile.id)
    name = profile.first_name or "agent"
    await update.message.reply_text(
        f"Verified ✅ Welcome {name}.\n\n"
        "_Note: typing your phone is less secure than the Share-my-phone button. "
        "Next time, tap the button to send your Telegram-verified contact._\n\n"
        "Try: _\"show me damac projects\"_ or _\"make a promo video for project 6\"_.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return True


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text or ""

    async with AsyncSessionLocal() as db:
        pid = await _get_linked_profile_id(db, chat_id)
        if pid is None:
            # Try typed phone as a verification fallback.
            if await _try_verify_by_typed_phone(update, db, chat_id, user_message):
                return
            await cmd_start(update, _ctx)
            return
        profile = await get_profile(db, pid)
        if not is_agent(profile):
            await update.message.reply_text(
                "Your access has been revoked. Please contact your admin."
            )
            return

        await update.message.chat.send_action("typing")
        result = await chat_turn(
            db,
            user_message=user_message,
            conversation_id=CHAT_CONV.get(chat_id),
            user_id=profile.id,
            channel="telegram",
            telegram_chat_id=chat_id,
        )
        CHAT_CONV[chat_id] = result["conversation_id"]

    reply = result["reply"] or "(no reply)"
    extras = _format_cards(result["cards"])

    # Send LLM body as plain text — Claude's stray markdown can break Telegram's parser.
    for chunk_start in range(0, len(reply), 4000):
        try:
            await update.message.reply_text(reply[chunk_start:chunk_start + 4000])
        except Exception as e:  # pragma: no cover — keep the chat alive
            log.warning("reply_text failed; sending stripped fallback: %s", e)
            await update.message.reply_text(reply[chunk_start:chunk_start + 4000].encode("ascii", "ignore").decode())

    if extras:
        try:
            await update.message.reply_text(extras, parse_mode="Markdown")
        except Exception as e:
            log.warning("cards reply with Markdown failed, retrying plain: %s", e)
            await update.message.reply_text(extras)


async def cmd_logout(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM messaging_links WHERE channel='telegram' AND external_id=:eid"),
            {"eid": str(chat_id)},
        )
        await db.commit()
    CHAT_CONV.pop(chat_id, None)
    await update.message.reply_text("Unlinked. Send /start to verify again.")


def main():
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not configured in .env")
    log.info("starting Telegram bot in polling mode")
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("logout", cmd_logout))
    application.add_handler(MessageHandler(filters.CONTACT, on_contact))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
