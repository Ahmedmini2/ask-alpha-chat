from datetime import datetime
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.db.models import Video

router = APIRouter(prefix="/v1/videos", tags=["videos"])


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    requested_by: UUID
    project_id: Optional[int] = None
    status: str
    ready: bool = False
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


@router.get("/{video_id}", response_model=VideoOut)
async def get_video(video_id: UUID, db: AsyncSession = Depends(get_db)):
    v = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="Video not found")
    # A video is deliverable ONLY when genuinely completed. During the Descript caption step
    # the row stays status='processing' with the RAW url already populated — so a poller hitting
    # this endpoint must not treat that as finished. Mirror the chat tool: prefer the captioned
    # version, and expose a url only once completed.
    completed = v.status == "completed"
    share_url = (v.captioned_video_url or v.video_url) if completed else None
    return VideoOut(
        id=v.id,
        requested_by=v.requested_by,
        project_id=v.project_id,
        status=v.status,
        ready=completed and bool(share_url),
        video_url=share_url,
        thumbnail_url=v.thumbnail_url if completed else None,
        error=v.error or v.caption_error,
        created_at=v.created_at,
        completed_at=v.completed_at,
    )
