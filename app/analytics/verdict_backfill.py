"""Backfill / refresh project_alpha_verdict for every priceable published project.

Each project is recomputed via alpha_verdict.recompute_verdict (own session, idempotent upsert),
so this is safe to re-run on the market-data refresh cadence. Run:

    python -m app.analytics.verdict_backfill            # all projects
    python -m app.analytics.verdict_backfill 50         # first 50 (smoke)
"""
import asyncio
import logging

from sqlalchemy import select

from app.analytics.alpha_verdict import recompute_verdict
from app.db.models import Project
from app.db.session import AsyncSessionLocal

log = logging.getLogger("askalpha.verdict_backfill")

_CHUNK = 200


async def backfill(concurrency: int = 8, limit: int | None = None) -> dict:
    async with AsyncSessionLocal() as db:
        q = (
            select(Project.id)
            .where(Project.is_published.is_(True), Project.min_price.isnot(None))
            .order_by(Project.id)
        )
        if limit:
            q = q.limit(limit)
        ids = [r[0] for r in (await db.execute(q)).all()]

    log.info("verdict backfill: %d projects (concurrency=%d)", len(ids), concurrency)
    sem = asyncio.Semaphore(concurrency)
    stats = {"ok": 0, "skipped": 0, "error": 0}

    async def _one(pid: int) -> None:
        async with sem:
            try:
                v = await recompute_verdict(pid)
                stats["ok" if v else "skipped"] += 1
            except Exception as e:  # pragma: no cover
                stats["error"] += 1
                log.warning("verdict %s failed: %s", pid, e)

    for i in range(0, len(ids), _CHUNK):
        await asyncio.gather(*[_one(p) for p in ids[i:i + _CHUNK]])
        log.info("progress %d/%d  ok=%d skipped=%d error=%d",
                 min(i + _CHUNK, len(ids)), len(ids), stats["ok"], stats["skipped"], stats["error"])

    log.info("verdict backfill done: %s", stats)
    return stats


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(backfill(limit=_limit))
