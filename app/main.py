import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import chat, projects, videos
from app.config import settings
from app.workers import heygen_poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("askalpha.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    poller_task: asyncio.Task | None = None
    if settings.heygen_api_key:
        poller_task = asyncio.create_task(heygen_poller.run_forever())
        log.info("HeyGen poller task spawned")
    else:
        log.info("HEYGEN_API_KEY not set; skipping HeyGen poller")
    try:
        yield
    finally:
        if poller_task is not None:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Ask Alpha API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.time()
    log.info("→ %s %s", request.method, request.url.path)
    response = await call_next(request)
    dur_ms = (time.time() - started) * 1000
    log.info("← %s %s  %d  %.0fms", request.method, request.url.path, response.status_code, dur_ms)
    return response


app.include_router(chat.router)
app.include_router(projects.router)
app.include_router(videos.router)


@app.get("/health")
def health():
    return {"status": "ok"}
