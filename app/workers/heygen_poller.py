import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
import httpx
from sqlalchemy import select, update
from app.config import settings
from app.brochures import storage
from app.db.models import Project, Video
from app.db.session import AsyncSessionLocal
from app.integrations import fal, heygen
from app.videos import align, broll, captions, outro

log = logging.getLogger("askalpha.heygen_poller")

POLL_INTERVAL_SEC = 10

# Caption (FAL transcribe + ffmpeg burn) concurrency. The ffmpeg burn itself is also bounded by
# the b-roll semaphore inside captions.burn_hormozi; this caps how many caption jobs run at once.
_caption_sem = asyncio.Semaphore(max(1, settings.descript_caption_concurrency))


def _captions_on() -> bool:
    return bool(settings.captions_enabled and settings.fal_key)


def _postprocess_on() -> bool:
    """Whether a finished HeyGen video needs our post-edit stage (b-roll and/or captions)."""
    return bool(settings.broll_enabled or _captions_on())


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


async def _set_broll(video_id: UUID, *, status: str,
                     url: Optional[str] = None, error: Optional[str] = None) -> None:
    """Record the b-roll outcome on the video row (its own columns; status is untouched)."""
    async with AsyncSessionLocal() as db:
        vals: dict = {"broll_status": status, "updated_at": datetime.now(timezone.utc)}
        if url is not None:
            vals["broll_video_url"] = url
        if error is not None:
            vals["broll_error"] = error
        await db.execute(update(Video).where(Video.id == video_id).values(**vals))
        await db.commit()


async def _maybe_broll(video_id: UUID, raw_url: str, project_id: Optional[int]) -> Optional[str]:
    """Best-effort b-roll edit. Returns the hosted URL of the composite to caption, or None to
    keep the raw HeyGen video. Records broll_status / broll_video_url / broll_error. Never raises."""
    if not settings.broll_enabled or project_id is None:
        return None
    try:
        src = await broll._fetch_bytes(raw_url)
        if not src:
            raise broll.BrollError("source download failed")
        aspect = await broll.detect_aspect(src)
        seed = int(video_id.hex[:8], 16)
        async with AsyncSessionLocal() as db:
            project = (await db.execute(
                select(Project).where(Project.id == project_id)
            )).scalar_one_or_none()
            name = project.name if project else "promo"
            blobs = (await broll.gather_broll_images(
                db, project, settings.broll_max_clips, aspect, seed=seed
            )) if project is not None else []
        if not blobs:
            await _set_broll(video_id, status="skipped")
            return None
        mp4 = await broll.add_broll(src, blobs, aspect, seed=seed)
        if mp4 is None:                                  # too short / no b-roll segments
            await _set_broll(video_id, status="skipped")
            return None
        _key, url = await storage.upload_mp4(mp4, name, storage.ASSETS_BUCKET)
        await _set_broll(video_id, status="done", url=url)
        log.info("video %s b-roll composited (%d clips)", video_id, len(blobs))
        return url
    except Exception as e:
        log.warning("video %s b-roll failed; captioning raw video: %s", video_id, e)
        await _set_broll(video_id, status="failed", error=str(e)[:1000])
        return None


async def _finalize(video_id: UUID, project_id: Optional[int], tg_chat_id: Optional[int], *,
                    deliver_url: str, caption_status: str,
                    caption_error: Optional[str] = None,
                    captioned_video_url: Optional[str] = None) -> None:
    """Mark the video completed, record the caption outcome, and send the single completion ping."""
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        vals: dict = {"status": "completed", "caption_status": caption_status,
                      "updated_at": now, "completed_at": now}
        if caption_error is not None:
            vals["caption_error"] = caption_error[:1000]
        if captioned_video_url is not None:
            vals["captioned_video_url"] = captioned_video_url
        await db.execute(update(Video).where(Video.id == video_id).values(**vals))
        await db.commit()
        pname = await _project_name(db, project_id)
    if tg_chat_id:
        label = f" — {pname}" if pname else ""
        suffix = " (with captions)" if caption_status == "done" else ""
        note = "\n(captions weren't added this time)" if caption_status == "failed" else ""
        await _notify_telegram(
            int(tg_chat_id),
            f"✅ Your video is ready{label}{suffix}{note}\nDownload / share:\n{deliver_url}",
        )


async def _maybe_outro(video_id: UUID, deliver_url: str, project_id: Optional[int]) -> Optional[str]:
    """Best-effort final step: append the orientation-correct Allegiance outro (with a short
    crossfade) to the video we're about to deliver. Returns the new hosted URL, or None to keep the
    original on ANY failure — the outro is a nicety and must never fail the job."""
    try:
        src = await broll._fetch_bytes(deliver_url)
        if not src:
            log.warning("outro: could not fetch %s for %s", deliver_url, video_id)
            return None
        merged = await outro.append_outro(src)
        async with AsyncSessionLocal() as db:
            pname = await _project_name(db, project_id)
        _key, url = await storage.upload_mp4(merged, f"{pname or 'promo'}-outro", storage.ASSETS_BUCKET)
        log.info("video %s outro appended", video_id)
        return url
    except Exception as e:
        log.warning("outro append failed for %s; delivering without outro: %s", video_id, e)
        return None


async def _broll_caption_and_finalize(
    video_id: UUID, raw_url: str, project_id: Optional[int], tg_chat_id: Optional[int],
    script: Optional[str] = None, add_outro: bool = False,
) -> None:
    """Post-process a finished HeyGen video: (1) cut in property b-roll, (2) burn Hormozi captions
    (FAL whisper timings + the ground-truth script for spelling), (3) append the Allegiance outro
    when the agent opted in. Every stage is best-effort — any failure falls back to the best video
    produced so far, so the job never fails. Sends exactly ONE completion notification."""
    try:
        # Phase 1 — b-roll. Its own concurrency guard, outside the caption semaphore.
        broll_url = await _maybe_broll(video_id, raw_url, project_id)
        source_url = broll_url or raw_url          # video we caption (composite if b-roll ran)
        deliver_url = broll_url or raw_url          # best video so far (b-roll or raw)
        caption_status = "skipped"
        caption_error: Optional[str] = None
        captioned_video_url: Optional[str] = None

        # Phase 2 — captions. Word timings come from the RAW audio (identical timeline to the
        # composite), then we burn them onto source_url.
        if _captions_on():
            async with _caption_sem:
                try:
                    words = await fal.transcribe_words(raw_url)
                    # Caption TEXT comes from the ground-truth script (correct brand spellings like
                    # "Damac"); whisper supplies only the per-word TIMING. Falls back to whisper's
                    # own transcription if alignment can't confidently map the two.
                    if script:
                        try:
                            words = align.align_script_to_words(script, words) or words
                        except Exception as ae:  # never let alignment fail the caption job
                            log.warning("caption align failed for %s; using whisper text: %s", video_id, ae)
                    mp4 = await captions.burn_hormozi(source_url, words)
                    async with AsyncSessionLocal() as db:
                        pname = await _project_name(db, project_id)
                    _key, captioned_url = await storage.upload_mp4(
                        mp4, pname or "promo", storage.ASSETS_BUCKET)
                    deliver_url = captioned_url
                    captioned_video_url = captioned_url
                    caption_status = "done"
                    log.info("video %s captioned via fal+ffmpeg", video_id)
                except Exception as e:
                    caption_status = "failed"
                    caption_error = str(e)
                    log.warning("captions failed for %s; delivering uncaptioned: %s", video_id, e)

        # Phase 3 — Allegiance outro (opt-in, best-effort), applied to whatever we're delivering.
        # Store the merged URL in captioned_video_url too: that's the column check_my_video_status
        # returns first, so the WEB "is my video ready?" path serves the outro version, not the
        # pre-outro one (Telegram already gets deliver_url in the completion message).
        if add_outro:
            outro_url = await _maybe_outro(video_id, deliver_url, project_id)
            if outro_url:
                deliver_url = outro_url
                captioned_video_url = outro_url

        await _finalize(video_id, project_id, tg_chat_id, deliver_url=deliver_url,
                        caption_status=caption_status, caption_error=caption_error,
                        captioned_video_url=captioned_video_url)
    except Exception as e:  # pragma: no cover — last-resort guard for the bg task
        log.exception("broll_caption_and_finalize crashed for %s: %s", video_id, e)


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
        to_caption: list[tuple] = []  # (video_id, raw_url, project_id, tg_chat_id, script, add_outro)

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
                if _postprocess_on() or v.add_outro:
                    # Hand off to the post-edit stage (b-roll + Hormozi captions + outro). The outro
                    # alone is enough to need this stage, so route here when the agent opted in even
                    # if b-roll and captions are off. caption_status='processing' takes the row out
                    # of the poll set (no double-spawn); status stays 'processing' (the only valid
                    # statuses are pending/processing/completed/failed). We store the raw video as a
                    # fallback and DON'T notify yet — _broll_caption_and_finalize sends the message.
                    await db.execute(update(Video).where(Video.id == v.id).values(
                        status="processing", caption_status="processing",
                        video_url=video_url, thumbnail_url=thumb, updated_at=now,
                    ))
                    touched += 1
                    to_caption.append((v.id, video_url, v.project_id, v.telegram_chat_id,
                                       v.script, v.add_outro))
                    log.info("video %s rendered, queuing post-edit (b-roll/captions/outro=%s)",
                             v.id, v.add_outro)
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

    # Spawn the b-roll + captioning finalizer AFTER the commit so the 'captioning' status is
    # durable first (keeps the row out of the next poll cycle — no double-spawn).
    for video_id, raw_url, project_id, tg_chat_id, script, add_outro in to_caption:
        asyncio.create_task(
            _broll_caption_and_finalize(video_id, raw_url, project_id, tg_chat_id, script, add_outro))

    return touched


async def run_forever():
    log.info("HeyGen poller started (interval=%ss); b-roll %s; Hormozi captions %s",
             POLL_INTERVAL_SEC,
             "ON" if settings.broll_enabled else "OFF",
             "ON" if _captions_on() else "OFF (set CAPTIONS_ENABLED + FAL_KEY)")
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("poll cycle error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SEC)
