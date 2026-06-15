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
            lines.append(f"\n📋 *Projects* (ranked by Alpha conviction — {len(items)} shown" +
                         (", more available" if c.get("has_more") else "") + "):")
            for p in items:
                price = ""
                if p.get("min_price") and p.get("max_price"):
                    price = f" — {p.get('currency') or ''} {int(p['min_price']):,}–{int(p['max_price']):,}"
                loc = p.get("city") or p.get("region") or ""
                dev = p.get("developer") or ""
                # Lead each card with its Alpha conviction score (the reason it ranks where it does).
                conv = p.get("conviction")
                if conv is not None:
                    vbadge = {"BUY": "🟢", "WATCH": "🟡", "SKIP": "🔴"}.get(p.get("verdict"), "•")
                    score = f" {vbadge} {(p.get('verdict') + ' ') if p.get('verdict') else ''}{conv}/100"
                else:
                    score = ""
                dist = f" · {p['distance_km']}km" if p.get("distance_km") is not None else ""
                lines.append(f"  •{score} *{p.get('name')}* ({dev}, {loc}){price}{dist}")
        elif kind == "investment_comparison":
            items = c.get("items", [])
            if items:
                lines.append("\n⚖️ *Head-to-head* (Alpha conviction on each):")
                for p in items:
                    conv = p.get("conviction")
                    if conv is not None:
                        vbadge = {"BUY": "🟢", "WATCH": "🟡", "SKIP": "🔴"}.get(p.get("verdict"), "•")
                        score = f" {vbadge} {(p.get('verdict') + ' ') if p.get('verdict') else ''}{conv}/100"
                    else:
                        score = ""
                    lines.append(f"  •{score} *{p.get('name')}*")
        elif kind == "document_quotes":
            items = c.get("items", [])
            if items:
                lines.append(f"\n📄 _Found {len(items)} brochure excerpts._")
        elif kind == "no_match_suggestions":
            items = c.get("items", [])
            if items:
                q = c.get("query") or "that"
                lines.append(f"\nWe don't have *{q}* in our system yet. Closest matches we do carry:")
                for p in items:
                    price = ""
                    if p.get("min_price") and p.get("max_price"):
                        price = f" — {p.get('currency') or ''} {int(p['min_price']):,}–{int(p['max_price']):,}"
                    loc = p.get("city") or p.get("region") or ""
                    dev = p.get("developer") or ""
                    lines.append(f"  • *{p.get('name')}* ({dev}, {loc}){price}")
        elif kind == "video_job":
            # No extra text: STEP 5's own reply already says the video is generating and the
            # captioned link will arrive automatically. The poller posts the link as its own
            # message when ready, so adding a card here would just clutter that single message.
            pass
        elif kind == "video_status":
            status = c.get("status")
            if c.get("ready") and c.get("video_url"):
                lines.append(
                    f"\n✅ *Your video is ready!*\n"
                    f"Download / share: {c.get('video_url')}"
                )
            elif status == "failed":
                detail = c.get("error_detail") or "unknown"
                lines.append(f"\n❌ *Video failed:* {detail}")
            else:
                lines.append(
                    "\n⏳ Still rendering. Try again in a minute."
                )
        elif kind == "alpha_verdict":
            name = c.get("project_name") or "This project"
            n = c.get("numbers") or {}
            badge = {"BUY": "🟢", "WATCH": "🟡", "SKIP": "🔴"}.get(c.get("verdict"), "•")
            header = f"\n{badge} *{name} — {c.get('verdict')}* ({c.get('conviction')}/100 conviction)"
            detail = []
            if n.get("net_yield_pct") is not None:
                detail.append(f"Net yield {n['net_yield_pct']}%")
            if n.get("annual_appreciation_pct") is not None:
                detail.append(f"Appreciation {n['annual_appreciation_pct']}%")
            if n.get("y5_value_aed"):
                detail.append(f"5-yr value AED {int(n['y5_value_aed']):,}")
            lines.append(header + ("\n" + " · ".join(detail) if detail else ""))
        elif kind == "live_market":
            name = c.get("community") or c.get("project_name") or "Area"
            bits = []
            if c.get("valuation"):
                bits.append(f"AVM AED {int(c['valuation']):,}")
            if c.get("ppsf_aed"):
                bits.append(f"{int(c['ppsf_aed'])}/sqft")
            if c.get("observed_yield_pct"):
                bits.append(f"yield {c['observed_yield_pct']}%")
            lines.append(f"\n📈 *{name} — live market* (Property Monitor)\n" + " · ".join(bits))
        elif kind == "brochure":
            name = c.get("project_name") or "Project"
            if c.get("sent_to_telegram"):
                # The PDF file itself was already pushed via sendDocument; just confirm.
                lines.append(f"\n📄 *{name} mini brochure* sent above as a PDF.")
            elif c.get("pdf_url"):
                lines.append(
                    f"\n📄 *{name} mini brochure is ready.*\n"
                    f"Download: {c.get('pdf_url')}"
                )
            else:
                lines.append(f"\n📄 The {name} brochure could not be delivered — please try again.")
        elif kind == "avatar_looks":
            # The look preview photos were already pushed (one per look, name as caption) by
            # the tool handler, and the assistant's own text asks the question — so nothing to
            # render here. Kept as an explicit branch so the card isn't silently dropped.
            pass
        elif kind == "inventory_export":
            label = c.get("label") or "Inventory"
            n = c.get("row_count")
            cap = " (capped — narrow the filters for the rest)" if c.get("truncated") else ""
            if c.get("sent_to_telegram"):
                lines.append(f"\n📊 *{label}* — {n} units sent above as an Excel sheet{cap}.")
            elif c.get("xlsx_url"):
                lines.append(f"\n📊 *{label}* — {n} units.\nDownload (Excel): {c.get('xlsx_url')}{cap}")
            else:
                lines.append(f"\n📊 The {label} export could not be delivered — please try again.")
        elif kind == "flyer":
            name = c.get("project_name") or "Project"
            label = c.get("flyer_label") or "Flyer"
            url = c.get("image_url")
            sent = c.get("sent_to_telegram")
            if sent and url:
                lines.append(f"\n📸 *{name} — {label}* sent above.\nDownload: {url}")
            elif sent:
                lines.append(f"\n📸 *{name} — {label}* sent above as an image.")
            elif url:
                lines.append(f"\n📸 *{name} — {label} flyer is ready.*\nDownload: {url}")
            else:
                lines.append(f"\n📸 The {name} flyer could not be delivered — please try again.")
        elif kind == "comparison_pdf":
            names = c.get("project_names") or []
            title = " vs ".join(names) if names else "Property comparison"
            if c.get("sent_to_telegram"):
                lines.append(f"\n📊 *Comparison — {title}* sent above as a PDF.")
            elif c.get("pdf_url"):
                lines.append(
                    f"\n📊 *Comparison ready — {title}.*\n"
                    f"Download: {c.get('pdf_url')}"
                )
            else:
                lines.append("\n📊 The comparison could not be delivered — please try again.")
        elif kind == "market_report":
            asof = c.get("as_of")
            title = f"Dubai Market Report{f' — {asof}' if asof else ''}"
            if c.get("sent_to_telegram"):
                lines.append(f"\n📊 *{title}* sent above as a PDF.")
            elif c.get("pdf_url"):
                lines.append(f"\n📊 *{title} is ready.*\nDownload: {c.get('pdf_url')}")
            else:
                lines.append("\n📊 The market report could not be delivered — please try again.")
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

    # A successfully-started video job is delivered as ONE message by the poller when the
    # captioned video is ready — so suppress the assistant's "it's generating…" acknowledgment
    # here. Otherwise the agent would get two messages (the ack now + the link later); they
    # only want the final captioned link.
    cards = result.get("cards") or []
    if any(c.get("type") == "video_job" for c in cards):
        return

    reply = result["reply"] or "(no reply)"
    extras = _format_cards(result["cards"])

    # ONE message per turn: combine the assistant's body with any card text and send it once
    # (previously the body and the cards went out as two separate messages). Try Markdown for the
    # card formatting; on any parser error, resend the SAME text as plain — still a single message.
    # Only the rare >4000-char turn is chunked.
    full = reply + ("\n" + extras if extras else "")
    for chunk_start in range(0, len(full), 4000):
        chunk = full[chunk_start:chunk_start + 4000]
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception as e:  # pragma: no cover — keep the chat alive
            log.warning("markdown send failed; resending plain: %s", e)
            try:
                await update.message.reply_text(chunk)
            except Exception:
                await update.message.reply_text(chunk.encode("ascii", "ignore").decode())


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
    # concurrent_updates lets a slow handler (e.g. a 30-60s brochure render) run
    # without blocking other users' messages, which PTB otherwise processes serially.
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("logout", cmd_logout))
    application.add_handler(MessageHandler(filters.CONTACT, on_contact))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
