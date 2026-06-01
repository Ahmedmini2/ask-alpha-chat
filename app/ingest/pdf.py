import asyncio
import io
import json
import boto3
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.core.embeddings import embed_text
from app.ingest.chunker import chunk_text

_s3 = boto3.client("s3", region_name=settings.aws_region)


def _download_from_s3(bucket: str, key: str) -> bytes:
    return _s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t:
            parts.append(t)
    return "\n\n".join(parts).strip()


async def ingest_asset(db: AsyncSession, asset_id: int) -> dict:
    """Download a project_assets row from S3, chunk it, embed, and store in rag_chunks.

    Returns a dict describing what happened. Idempotent: existing chunks for this asset
    are deleted before re-inserting. Blocking boto3 / pypdf calls run in a thread so
    multiple concurrent invocations actually parallelise.
    """
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

    pdf_bytes = await asyncio.to_thread(_download_from_s3, row["s3_bucket"], row["s3_key"])
    raw_text = await asyncio.to_thread(_extract_pdf_text, pdf_bytes)
    if not raw_text:
        return {"asset_id": asset_id, "status": "skipped", "reason": "no_text"}

    chunks = chunk_text(raw_text)
    if not chunks:
        return {"asset_id": asset_id, "status": "skipped", "reason": "no_chunks"}

    await db.execute(
        text("DELETE FROM rag_chunks WHERE asset_id = :id"),
        {"id": asset_id},
    )

    meta = json.dumps({"s3_bucket": row["s3_bucket"], "s3_key": row["s3_key"]})
    for i, content in enumerate(chunks):
        vec = await asyncio.to_thread(embed_text, content)
        await db.execute(
            text("""
                INSERT INTO rag_chunks
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
    }
