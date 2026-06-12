"""Drive the Remotion CLI (remotion/) to burn captions onto a video.

We shell out to `remotion render` rather than embedding a JS runtime: Remotion is
a Node/React renderer, and the CLI is its supported headless entry point. One
render at a time (an asyncio.Lock), mirroring app/brochures/render.py's bounded
Chromium use on the small instance.
"""
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from app.config import settings

log = logging.getLogger("askalpha.captioning")

# repo-root/remotion (this file is app/captioning/render.py).
REMOTION_DIR = Path(__file__).resolve().parent.parent.parent / "remotion"
COMPOSITION_ID = "CaptionedVideo"
ENTRY_POINT = "src/index.ts"
RENDER_TIMEOUT_SEC = 600  # 10 min ceiling for a ~60s reel

_render_lock = asyncio.Lock()


class RenderError(Exception):
    pass


def _render_env() -> dict:
    """Local-dev (WSL) fallback: reuse the Chromium shared libs the brochure
    renderer extracts. Production (Docker) doesn't have this dir."""
    env = dict(os.environ)
    local_libs = Path.home() / ".cache" / "chromium-local-libs"
    if local_libs.is_dir():
        prev = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{local_libs}:{prev}" if prev else str(local_libs)
    return env


async def render_captioned(src: str, captions: list[dict]) -> bytes:
    """Render the source video (URL or file path) with captions burned in.

    `src` is handed straight to <OffthreadVideo> — a public https URL is most
    reliable. Returns the finished MP4 bytes.
    """
    if not REMOTION_DIR.is_dir():
        raise RenderError(f"Remotion project not found at {REMOTION_DIR}")

    async with _render_lock:
        with tempfile.TemporaryDirectory(prefix="captions-") as tmp:
            tmp_path = Path(tmp)
            props_file = tmp_path / "props.json"
            out_file = tmp_path / "captioned.mp4"
            props_file.write_text(
                json.dumps({"src": src, "captions": captions}), encoding="utf-8"
            )

            cmd = [
                "npx", "remotion", "render", ENTRY_POINT, COMPOSITION_ID,
                str(out_file),
                f"--props={props_file}",
                "--concurrency=2",
                "--log=error",
            ]
            exe = settings.remotion_browser_executable
            if exe:
                cmd.append(f"--browser-executable={exe}")

            log.info("remotion render start src=%s captions=%d", src[:80], len(captions))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(REMOTION_DIR),
                env=_render_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                proc.kill()
                raise RenderError(f"remotion render timed out after {RENDER_TIMEOUT_SEC}s")

            if proc.returncode != 0:
                raise RenderError(
                    f"remotion render failed (exit {proc.returncode}): "
                    f"{err.decode('utf-8', 'ignore')[-800:]}"
                )
            if not out_file.exists():
                raise RenderError("remotion render produced no output file")
            data = out_file.read_bytes()
            log.info("remotion render done %dKB", len(data) // 1024)
            return data
