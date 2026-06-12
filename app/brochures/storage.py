"""S3 helpers for brochure generation.

Asset images live in our private bucket (project_assets.s3_bucket/s3_key — the
s3_url column is unpopulated, and Reelly source_url must never be used). We pull
bytes with GetObject and write the finished PDF back under generated/brochures/.
"""
import asyncio
import logging
import re
import uuid

import boto3

from app.config import settings

log = logging.getLogger("askalpha.brochures")

# The assets bucket lives in eu-west-2 (same constraint as Textract ingestion).
_s3 = boto3.client("s3", region_name=settings.s3_assets_region)

PDF_KEY_PREFIX = "generated/brochures"
PRESIGN_TTL_SEC = 7 * 24 * 3600  # 7 days — the SigV4 maximum


def _get_object_bytes(bucket: str, key: str) -> bytes:
    return _s3.get_object(Bucket=bucket, Key=key)["Body"].read()


async def fetch_asset_bytes(bucket: str, key: str) -> bytes | None:
    """Download one S3 object; returns None instead of raising on failure."""
    try:
        return await asyncio.to_thread(_get_object_bytes, bucket, key)
    except Exception as e:
        log.warning("asset fetch failed s3://%s/%s: %s", bucket, key, e)
        return None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "brochure").lower()).strip("-")
    return s[:60] or "brochure"


async def upload_pdf(pdf_bytes: bytes, project_name: str, bucket: str) -> tuple[str, str]:
    """Store the rendered PDF; returns (s3_key, presigned_url)."""
    key = f"{PDF_KEY_PREFIX}/{slugify(project_name)}-{uuid.uuid4().hex[:8]}.pdf"

    def _put_and_sign() -> str:
        _s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
            ContentDisposition=f'attachment; filename="{slugify(project_name)}-mini-brochure.pdf"',
        )
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_TTL_SEC,
        )

    url = await asyncio.to_thread(_put_and_sign)
    return key, url
