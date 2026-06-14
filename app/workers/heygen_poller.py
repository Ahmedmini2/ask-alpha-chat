import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
import httpx
from sqlalchemy import select, update
from app.config import settings
from app.db.models import Project, Video
from app.db.session import AsyncSessionLocal
from app.integrations import descript, heygen

log = logging.getLogger("askalpha.heygen_poller")

POLL_INTERVAL_SEC = 10

# Descript caption jobs are heavy + slow (import → agent → publish); bound concurrency.
_caption_sem = asyncio.Semaphore(max(1, settings.descript_caption_concurrency))


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


async def _caption_and_finalize(
    video_id: UUID, raw_url: str, project_id: Optional[int], tg_chat_id: Optional[int]
) -> None:
    """Send a finished HeyGen video through Descript for captions, then mark it complete and
    notify. Best-effort: ANY failure falls back to the uncaptioned HeyGen video so the agent
    always gets something. Fire-and-forget; the outer try keeps a crash from surfacing as an
    unretrieved-task warning."""
    async with _caption_sem:
        try:
            try:
                captioned_url = await descript.caption_video(raw_url)
            except Exception as e:
                log.warning("descript captioning failed for %s; delivering raw video: %s", video_id, e)
                async with AsyncSessionLocal() as db:
                    now = datetime.now(timezone.utc)
                    await db.execute(update(Video).where(Video.id == video_id).values(
                        status="completed", caption_status="failed", caption_error=str(e)[:1000],
                        updated_at=now, completed_at=now,
                    ))
                    await db.commit()
                    pname = await _project_name(db, project_id)
                if tg_chat_id:
                    label = f" — {pname}" if pname else ""
                    await _notify_telegram(
                        int(tg_chat_id),
                        f"✅ Your video is ready{label}\n(captions weren't added this time)\n"
                        f"Download / share:\n{raw_url}",
                    )
                return

            async with AsyncSessionLocal() as db:
                now = datetime.now(timezone.utc)
                await db.execute(update(Video).where(Video.id == video_id).values(
                    status="completed", caption_status="done",
                    captioned_video_url=captioned_url, updated_at=now, completed_at=now,
                ))
                await db.commit()
                pname = await _project_name(db, project_id)
            log.info("video %s captioned via descript", video_id)
            if tg_chat_id:
                label = f" — {pname}" if pname else ""
                await _notify_telegram(
                    int(tg_chat_id),
                    f"✅ Your video is ready{label} (with captions)\nDownload / share:\n{captioned_url}",
                )
        except Exception as e:  # pragma: no cover — last-resort guard for the bg task
            log.exception("caption_and_finalize crashed for %s: %s", video_id, e)


async def _poll_once() -> int:
    """Check every video in (pending, processing) and update if finished. Returns count touched."""
    async with AsyncSessionLocal() as db:
        # caption_status='processing' marks a video that's already been handed to the
        # Descript caption step — exclude it so we never re-spawn captioning (which would
        # double-notify). We keep status='processing' during captioning (the DB status
        # CHECK only allows pending/processing/completed/failed — no 'captioning'), so the
        # caption_status guard is what takes it out of the poll set.
        rows = (await db.execute(
            select(Video).where(
                Video.status.in_(("pending", "processing")),
                Video.caption_status.is_distinct_from("processing"),
            )
        )).scalars().all()
        if rows:
            log.info("poll cycle: %d video(s) in flight", len(rows))

        touched = 0
        notifications: list[tuple[int, str]] = []  # (chat_id, text)
        to_caption: list[tuple] = []  # (video_id, raw_url, project_id, tg_chat_id)

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
                if not video_url:
                    # Completed but the URL isn't populated yet — pick it up next cycle.
                    continue
                if settings.descript_api_token:
                    # Hand off to the Descript caption post-step. caption_status='processing'
                    # takes the row out of the poll set (no double-spawn); status stays
                    # 'processing' (the only valid statuses are pending/processing/completed/
                    # failed). We store the raw video as a fallback and DON'T notify yet —
                    # _caption_and_finalize sends the single completion message.
                    await db.execute(update(Video).where(Video.id == v.id).values(
                        status="processing", caption_status="processing",
                        video_url=video_url, thumbnail_url=thumb, updated_at=now,
                    ))
                    touched += 1
                    to_caption.append((v.id, video_url, v.project_id, v.telegram_chat_id))
                    log.info("video %s rendered, queuing Descript captions", v.id)
                else:
                    await db.execute(update(Video).where(Video.id == v.id).values(
                        status="completed", video_url=video_url, thumbnail_url=thumb,
                        updated_at=now, completed_at=now,
                    ))
                    touched += 1
                    log.info("video %s completed url=%s", v.id, video_url)
                    if v.telegram_chat_id:
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

    # Spawn captioning AFTER the commit so the 'captioning' status is durable first
    # (keeps the row out of the next poll cycle — no double-spawn).
    for video_id, raw_url, project_id, tg_chat_id in to_caption:
        asyncio.create_task(_caption_and_finalize(video_id, raw_url, project_id, tg_chat_id))

    return touched


async def run_forever():
    log.info("HeyGen poller started (interval=%ss); Descript captioning %s",
             POLL_INTERVAL_SEC,
             "ENABLED" if settings.descript_api_token else "DISABLED — DESCRIPT_API_TOKEN not loaded")
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("poll cycle error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SEC)
