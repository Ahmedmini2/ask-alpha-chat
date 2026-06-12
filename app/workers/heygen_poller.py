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
from app.integrations import heygen
from app.captioning import pipeline as caption_pipeline
from app.captioning import storage as caption_storage

log = logging.getLogger("askalpha.heygen_poller")

POLL_INTERVAL_SEC = 10

# Bound captioning jobs (download + whisper + Remotion render) — each is CPU/RAM-heavy
# and we share the box with the API / Telegram bot. Default 1 at a time.
_caption_sem = asyncio.Semaphore(max(1, settings.caption_render_concurrency))


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


async def _send_telegram_video(chat_id: int, mp4_bytes: bytes, filename: str, caption: str) -> bool:
    """Push the captioned MP4 straight into the chat (plays inline)."""
    if not settings.telegram_bot_token:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendVideo"
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                url,
                data={"chat_id": str(chat_id), "caption": caption[:1024], "supports_streaming": "true"},
                files={"video": (filename, mp4_bytes, "video/mp4")},
            )
            if r.status_code >= 400:
                log.warning("telegram sendVideo failed %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        log.warning("telegram sendVideo error: %s", e)
        return False


async def _project_name(db, project_id: Optional[int]) -> str:
    if project_id is None:
        return ""
    name = (await db.execute(
        select(Project.name).where(Project.id == project_id)
    )).scalar_one_or_none()
    return name or ""


async def caption_and_finalize(
    video_id: UUID, raw_url: str, project_id: Optional[int], tg_chat_id: Optional[int]
) -> None:
    """Burn Hormozi captions onto a finished HeyGen video, then deliver it.

    Best-effort: any failure falls back to the raw (uncaptioned) HeyGen video so
    the agent always receives something. Runs as a fire-and-forget task; the outer
    try keeps a crash from becoming an unretrieved-task-exception warning.
    """
    async with _caption_sem:
        try:
            async with AsyncSessionLocal() as db:
                pname = await _project_name(db, project_id)
                now = datetime.now(timezone.utc)

                # 1) Render captions (download → whisper → Remotion).
                try:
                    mp4 = await caption_pipeline.caption_video(raw_url)
                except Exception as e:
                    log.warning("captioning failed for %s; delivering raw video: %s", video_id, e)
                    await db.execute(update(Video).where(Video.id == video_id).values(
                        status="completed", caption_status="failed", caption_error=str(e)[:1000],
                        updated_at=now, completed_at=now,
                    ))
                    await db.commit()
                    if tg_chat_id:
                        label = f" — {pname}" if pname else ""
                        await _notify_telegram(
                            int(tg_chat_id),
                            f"✅ Your video is ready{label}\n(captions weren't added this time)\n"
                            f"Download / share:\n{raw_url}",
                        )
                    return

                # 2) Upload for a shareable link (best-effort: Telegram works without it).
                captioned_url = None
                try:
                    _key, captioned_url = await caption_storage.upload_video(mp4, pname or "promo")
                except Exception as e:
                    log.error("captioned S3 upload failed (continuing with Telegram only): %s", e)

                # 3) Persist.
                await db.execute(update(Video).where(Video.id == video_id).values(
                    status="completed", caption_status="done",
                    captioned_video_url=captioned_url,
                    updated_at=now, completed_at=now,
                ))
                await db.commit()
                log.info("video %s captioned url=%s telegram=%s", video_id, captioned_url, bool(tg_chat_id))

                # 4) Deliver on Telegram: the file inline + a download link.
                if tg_chat_id:
                    label = f" — {pname}" if pname else ""
                    filename = f"{caption_storage.slugify(pname or 'promo')}-promo.mp4"
                    sent = await _send_telegram_video(
                        int(tg_chat_id), mp4, filename,
                        caption=f"🎬 Your video is ready{label} (with captions)",
                    )
                    if captioned_url:
                        await _notify_telegram(int(tg_chat_id), f"Download / share:\n{captioned_url}")
                    elif not sent:
                        # Neither S3 nor inline upload worked — fall back to the raw link.
                        await _notify_telegram(
                            int(tg_chat_id),
                            f"✅ Your video is ready{label}\nDownload / share:\n{raw_url}",
                        )
        except Exception as e:  # pragma: no cover — last-resort guard for the bg task
            log.exception("caption_and_finalize crashed for %s: %s", video_id, e)


async def _poll_once() -> int:
    """Check every video in (pending, processing) and update if finished. Returns count touched."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Video).where(Video.status.in_(("pending", "processing")))
        )).scalars().all()

        touched = 0
        notifications: list[tuple[int, str]] = []  # (chat_id, text)
        to_caption: list[tuple[UUID, str, Optional[int], Optional[int]]] = []

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
                    # Completed but URL not populated yet — pick it up next cycle.
                    continue
                # Hand off to the caption post-step. status='captioning' takes the row
                # out of the poll set (no double-spawn); store the raw video as fallback
                # and DON'T notify yet — caption_and_finalize does that.
                await db.execute(update(Video).where(Video.id == v.id).values(
                    status="captioning",
                    caption_status="processing",
                    video_url=video_url,
                    thumbnail_url=thumb,
                    updated_at=now,
                ))
                touched += 1
                log.info("video %s rendered, queuing captions url=%s", v.id, video_url)
                to_caption.append((v.id, video_url, v.project_id, v.telegram_chat_id))
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

    # Spawn captioning AFTER the commit so the 'captioning' status is durable first.
    for video_id, raw_url, project_id, tg_chat_id in to_caption:
        asyncio.create_task(caption_and_finalize(video_id, raw_url, project_id, tg_chat_id))

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
