import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    if settings.heygen_api_key and settings.run_heygen_poller:
        poller_task = asyncio.create_task(heygen_poller.run_forever())
        log.info("HeyGen poller task spawned")
    elif not settings.run_heygen_poller:
        log.info("RUN_HEYGEN_POLLER=false; poller not started here (runs in the worker task)")
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


# Request logging + a catch-all error guard. Registered BEFORE the CORS middleware ON
# PURPOSE: middleware added later is the OUTER layer, so CORS ends up wrapping this one.
# That ordering is the whole fix. An unhandled error that escapes a route's own
# try/except — a Supabase connection blip raised inside the get_db dependency, a
# response-model serialization error, etc. — would otherwise bubble all the way out to
# Starlette's ServerErrorMiddleware, which sits OUTSIDE CORS and emits a bare 500 with
# NO Access-Control-Allow-Origin header. A browser can't read a cross-origin response
# that lacks that header, so the fetch() rejects with the opaque "TypeError: Failed to
# fetch" instead of the real status/body. By catching the exception HERE (inside CORS)
# and returning a normal JSONResponse, the 500 flows back out through the CORS layer,
# picks up its headers, and the frontend sees a readable error rather than a failed fetch.
@app.middleware("http")
async def log_and_guard(request: Request, call_next):
    started = time.time()
    log.info("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        dur_ms = (time.time() - started) * 1000
        log.exception("✗ %s %s  500  %.0fms (unhandled)", request.method, request.url.path, dur_ms)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
    dur_ms = (time.time() - started) * 1000
    log.info("← %s %s  %d  %.0fms", request.method, request.url.path, response.status_code, dur_ms)
    return response


_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(chat.router)
app.include_router(projects.router)
app.include_router(videos.router)


@app.get("/health")
def health():
    return {"status": "ok"}
