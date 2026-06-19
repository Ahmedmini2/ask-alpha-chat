"""Stitch several cinematic segment clips into one continuous MP4.

HeyGen's Cinematic Avatar (Seedance) caps a single clip at ~15s, so a 30s/45s cinematic video is
generated as 2–3 separate 15s clips. Once they've all rendered, the poller calls
`concat_clips([seg0_bytes, seg1_bytes, ...])` to join them in order. Each clip is first normalised to
the FIRST clip's resolution/fps with a guaranteed stereo audio track (reusing outro._normalize), then
concatenated with the ffmpeg concat filter (re-encode — robust to minor stream differences between
independently-generated Seedance clips). The merged clip then flows into the SAME caption + outro
post-edit as every other video.

Best-effort by contract: raises StitchError on failure so the caller can fail the job cleanly.
Requires ffmpeg + ffprobe on PATH (installed in the Dockerfile), like b-roll/captions/outro.
"""
import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from app.videos import broll, outro

log = logging.getLogger("askalpha.stitch")


class StitchError(Exception):
    pass


def _concat_sync(clips: list[bytes], cfg: "broll.BrollConfig") -> bytes:
    clips = [c for c in clips if c]
    if not clips:
        raise StitchError("no clips to stitch")

    with TemporaryDirectory(prefix="stitch-") as tmp:
        tmpp = Path(tmp)
        raw_paths = []
        for i, b in enumerate(clips):
            p = tmpp / f"seg{i}.mp4"
            p.write_bytes(b)
            raw_paths.append(p)

        # All segments share the same cinematic aspect/resolution, but normalise to the FIRST clip's
        # exact w×h×fps + a stereo AAC track so the concat filter gets identical stream params.
        w, h, fps, _dur = broll._probe(str(raw_paths[0]), cfg)
        norm_paths = []
        for i, p in enumerate(raw_paths):
            n = tmpp / f"n{i}.mp4"
            outro._normalize(str(p), str(n), w, h, fps, cfg)
            norm_paths.append(n)

        if len(norm_paths) == 1:
            data = norm_paths[0].read_bytes()
            if not data:
                raise StitchError("ffmpeg produced empty output")
            return data

        out_path = tmpp / "stitched.mp4"
        inputs: list[str] = []
        for n in norm_paths:
            inputs += ["-i", str(n)]
        streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(norm_paths)))
        fc = f"{streams}concat=n={len(norm_paths)}:v=1:a=1[v][a]"
        args = [cfg.ffmpeg, "-y", *inputs, "-filter_complex", fc,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out_path)]
        broll._run_ffmpeg(args, cfg.timeout_sec)

        data = out_path.read_bytes()
        if not data:
            raise StitchError("ffmpeg produced empty output")
        return data


async def concat_clips(clips: list[bytes], *, cfg: Optional["broll.BrollConfig"] = None) -> bytes:
    """Join cinematic segment clips (in order) into one MP4. Raises StitchError on failure."""
    cfg = cfg or broll.BrollConfig.from_settings()
    async with broll._sem:   # share the b-roll/outro ffmpeg concurrency guard
        return await asyncio.to_thread(_concat_sync, clips, cfg)
