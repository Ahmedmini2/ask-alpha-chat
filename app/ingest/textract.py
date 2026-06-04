import asyncio
import json
import time
import boto3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.core.embeddings import embed_text
from app.ingest.chunker import chunk_text

_textract = boto3.client("textract", region_name=settings.s3_assets_region)

POLL_INTERVAL_SEC = 5
MAX_POLLS = 60  # 5 minutes


def _start_job(bucket: str, key: str) -> str:
    resp = _textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    return resp["JobId"]


def _wait_and_collect(job_id: str) -> str | None:
    """Poll until job finishes, then collect all LINE block text. Returns None on failure/timeout."""
    result: dict = {}
    for _ in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL_SEC)
        result = _textract.get_document_text_detection(JobId=job_id)
        status = result.get("JobStatus")
        if status == "SUCCEEDED":
            break
        if status == "FAILED":
            return None
    else:
        return None  # timed out

    lines: list[str] = []
    for block in result.get("Blocks", []):
        if block.get("BlockType") == "LINE" and block.get("Text"):
            lines.append(block["Text"])

    next_token = result.get("NextToken")
    while next_token:
        result = _textract.get_document_text_detection(JobId=job_id, NextToken=next_token)
        for block in result.get("Blocks", []):
            if block.get("BlockType") == "LINE" and block.get("Text"):
                lines.append(block["Text"])
        next_token = result.get("NextToken")

    return "\n".join(lines).strip() or None


async def ocr_asset(db: AsyncSession, asset_id: int) -> dict:
    """Run Textract OCR on an asset's S3 PDF and store chunks. Idempotent."""
    row = (await db.execute(
        text("""
            SELECT id, project_id, kind, s3_bucket, s3_key
            FROM project_assets WHERE id = :id
        """),
        {"id": asset_id},
    )).mappings().first()
    if row is None:
        return {"asset_id": asset_id, "status": "skipped", "reason": "not_found"}
    if not row["s3_bucket"] or not row["s3_key"]:
        return {"asset_id": asset_id, "status": "skipped", "reason": "no_s3"}

    job_id = await asyncio.to_thread(_start_job, row["s3_bucket"], row["s3_key"])
    raw_text = await asyncio.to_thread(_wait_and_collect, job_id)
    if not raw_text:
        return {"asset_id": asset_id, "status": "skipped", "reason": "ocr_no_text"}

    chunks = chunk_text(raw_text)
    if not chunks:
        return {"asset_id": asset_id, "status": "skipped", "reason": "no_chunks"}

    await db.execute(
        text("DELETE FROM document_chunks WHERE asset_id = :id"),
        {"id": asset_id},
    )

    meta = json.dumps({
        "s3_bucket": row["s3_bucket"],
        "s3_key": row["s3_key"],
        "ocr": True,
    })
    for i, content in enumerate(chunks):
        vec = await asyncio.to_thread(embed_text, content)
        await db.execute(
            text("""
                INSERT INTO document_chunks
                    (project_id, asset_id, source_kind, chunk_index, content, embedding, metadata)
                VALUES
                    (:pid, :aid, :sk, :ci, :content, CAST(:embedding AS vector), CAST(:meta AS jsonb))
            """),
            {
                "pid": row["project_id"],
                "aid": asset_id,
                "sk": row["kind"],
                "ci": i,
                "content": content,
                "embedding": str(vec),
                "meta": meta,
            },
        )
    await db.commit()
    return {
        "asset_id": asset_id,
        "project_id": row["project_id"],
        "status": "ok",
        "chunks": len(chunks),
        "chars": len(raw_text),
        "via": "ocr",
    }
