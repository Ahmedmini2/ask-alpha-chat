import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import httpx
from sqlalchemy import select, update
from app.config import settings
from app.db.models import Project, Video
from app.db.session import AsyncSessionLocal
from app.integrations import heygen

log = logging.getLogger("askalpha.heygen_poller")

POLL_INTERVAL_SEC = 10


async def _notify_telegram(chat_id: int, text: str) -> None:
    if not settings.telegram_bot_token:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False})
            if r.status_code >= 400:
                log.warning("telegram notify failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("telegram notify error: %s", e)


async def _project_name(db, project_id: Optional[int]) -> str:
    if project_id is None:
        return ""
    name = (await db.execute(
        select(Project.name).where(Project.id == project_id)
    )).scalar_one_or_none()
    return name or ""


async def _poll_once() -> int:
    """Check every video in (pending, processing) and update if finished. Returns count touched."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Video).where(Video.status.in_(("pending", "processing")))
        )).scalars().all()

        touched = 0
        notifications: list[tuple[int, str]] = []  # (chat_id, text)

        for v in rows:
            if not v.heygen_video_id:
                continue
            try:
                payload = await heygen.get_video_status(v.heygen_video_id)
            except heygen.HeyGenError as e:
                log.warning("status check failed for %s: %s", v.id, e)
                continue

            remote_status = (payload.get("status") or "").lower()
            now = datetime.now(timezone.utc)

            if remote_status == "completed":
                video_url = payload.get("video_url")
                thumb = payload.get("thumbnail_url")
                await db.execute(update(Video).where(Video.id == v.id).values(
                    status="completed",
                    video_url=video_url,
                    thumbnail_url=thumb,
                    updated_at=now,
                    completed_at=now,
                ))
                touched += 1
                log.info("video %s completed url=%s", v.id, video_url)
                if v.telegram_chat_id and video_url:
                    pname = await _project_name(db, v.project_id)
                    label = f" — {pname}" if pname else ""
                    notifications.append((
                        int(v.telegram_chat_id),
                        f"✅ Your video is ready{label}\nDownload / share:\n{video_url}",
                    ))
            elif remote_status == "failed":
                err_detail = str(payload.get("error") or "HeyGen reported failure")
                await db.execute(update(Video).where(Video.id == v.id).values(
                    status="failed", error=err_detail, updated_at=now,
                ))
                touched += 1
                log.warning("video %s failed: %s", v.id, err_detail)
                if v.telegram_chat_id:
                    pname = await _project_name(db, v.project_id)
                    label = f" for {pname}" if pname else ""
                    notifications.append((
                        int(v.telegram_chat_id),
                        f"❌ Your video{label} failed: {err_detail[:300]}",
                    ))
            elif remote_status and remote_status != v.status:
                await db.execute(update(Video).where(Video.id == v.id).values(
                    status=remote_status, updated_at=now,
                ))
                touched += 1

        if touched:
            await db.commit()

    # Send Telegram pings AFTER the DB commit so we don't double-notify on retry.
    for chat_id, text in notifications:
        await _notify_telegram(chat_id, text)

    return touched


async def run_forever():
    log.info("HeyGen poller started (interval=%ss)", POLL_INTERVAL_SEC)
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("poll cycle error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SEC)
