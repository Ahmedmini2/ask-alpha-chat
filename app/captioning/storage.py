"""S3 helpers for captioned videos — mirrors app/brochures/storage.py.

Writes the finished MP4 under generated/videos/ in the same private assets bucket
and returns a presigned download URL. Needs s3:PutObject on the bucket (the same
pending grant as brochures, extended to generated/videos/*); Telegram delivery
works without it.
"""
import asyncio
import logging
import re
import uuid

import boto3

from app.config import settings

log = logging.getLogger("askalpha.captioning")

ASSETS_BUCKET = "assets-allegiance"
VIDEO_KEY_PREFIX = "generated/videos"
PRESIGN_TTL_SEC = 7 * 24 * 3600  # 7 days — the SigV4 maximum

_s3 = boto3.client("s3", region_name=settings.s3_assets_region)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "video").lower()).strip("-")
    return s[:60] or "video"


async def upload_video(mp4_bytes: bytes, project_name: str) -> tuple[str, str]:
    """Store the captioned MP4; returns (s3_key, presigned_url)."""
    slug = slugify(project_name)
    key = f"{VIDEO_KEY_PREFIX}/{slug}-{uuid.uuid4().hex[:8]}.mp4"

    def _put_and_sign() -> str:
        _s3.put_object(
            Bucket=ASSETS_BUCKET,
            Key=key,
            Body=mp4_bytes,
            ContentType="video/mp4",
            ContentDisposition=f'attachment; filename="{slug}-promo.mp4"',
        )
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": ASSETS_BUCKET, "Key": key},
            ExpiresIn=PRESIGN_TTL_SEC,
        )

    url = await asyncio.to_thread(_put_and_sign)
    return key, url
