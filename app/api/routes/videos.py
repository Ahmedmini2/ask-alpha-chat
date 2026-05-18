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
    return v
