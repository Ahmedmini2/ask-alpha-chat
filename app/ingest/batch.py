import asyncio
import time
from collections import Counter
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.ingest.pdf import ingest_asset
from app.ingest.textract import ocr_asset


async def _pending_asset_ids(kind: str) -> list[int]:
    """Return asset IDs of stored PDFs of the given kind that don't yet have chunks."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("""
                SELECT a.id
                FROM project_assets a
                LEFT JOIN rag_chunks c ON c.asset_id = a.id
                WHERE a.kind = :kind
                  AND a.status = 'stored'
                  AND a.mime_type = 'application/pdf'
                  AND c.id IS NULL
                GROUP BY a.id
                ORDER BY a.id
            """),
            {"kind": kind},
        )).all()
    return [r[0] for r in rows]


async def _run_one(asset_id: int, mode: str) -> dict:
    handler = ocr_asset if mode == "ocr" else ingest_asset
    async with AsyncSessionLocal() as db:
        try:
            return await handler(db, asset_id)
        except Exception as e:
            return {"asset_id": asset_id, "status": "error", "err": f"{type(e).__name__}: {e}"}


async def run_batch(kind: str = "brochure", concurrency: int = 8, log_every: int = 25, mode: str = "pdf") -> dict:
    asset_ids = await _pending_asset_ids(kind)
    total = len(asset_ids)
    print(f"[batch] mode={mode} kind={kind} pending={total} concurrency={concurrency}")
    if total == 0:
        return {"total": 0, "stats": {}}

    sem = asyncio.Semaphore(concurrency)
    stats: Counter = Counter()
    done = 0
    started = time.time()
    failures: list[dict] = []

    async def worker(aid: int):
        nonlocal done
        async with sem:
            result = await _run_one(aid, mode)
        done += 1
        status = result.get("status", "unknown")
        reason = result.get("reason") or result.get("err") or ""
        key = status if status == "ok" else f"{status}:{reason[:40]}"
        stats[key] += 1
        if status not in ("ok", "skipped"):
            failures.append(result)
        if done % log_every == 0 or done == total:
            elapsed = time.time() - started
            rate = done / elapsed if elapsed else 0
            eta = (total - done) / rate if rate else 0
            print(f"[batch] {done}/{total}  rate={rate:.2f}/s  eta={eta/60:.1f}min  stats={dict(stats)}")
        return result

    await asyncio.gather(*(worker(aid) for aid in asset_ids))
    elapsed = time.time() - started
    print(f"[batch] DONE in {elapsed/60:.1f}min  stats={dict(stats)}")
    if failures:
        print(f"[batch] {len(failures)} errors; first 5:")
        for f in failures[:5]:
            print(f"        {f}")
    return {"total": total, "elapsed_sec": elapsed, "stats": dict(stats)}


if __name__ == "__main__":
    import sys
    kind = sys.argv[1] if len(sys.argv) > 1 else "brochure"
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    asyncio.run(run_batch(kind=kind, concurrency=concurrency))
