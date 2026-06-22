"""Standalone entrypoint for the HeyGen poller — the dedicated ECS `worker` service.

On Railway everything ran in one process and the API's lifespan spawned the poller
(see app/main.py). On ECS the API is horizontally autoscaled, so the poller is moved OUT
of the API (RUN_HEYGEN_POLLER=false there) and run here as a SINGLE always-on task: the
poller must be a singleton or it duplicate-processes finished videos (b-roll/captions)
and races the videos table.

Run with:  python -m app.workers.run_poller
"""
import asyncio
import logging

from app.config import settings
from app.workers import heygen_poller

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("askalpha.worker")


def main() -> None:
    if not settings.heygen_api_key:
        log.error("HEYGEN_API_KEY not set — nothing to poll; exiting.")
        return
    log.info("HeyGen poller worker starting")
    asyncio.run(heygen_poller.run_forever())


if __name__ == "__main__":
    main()
