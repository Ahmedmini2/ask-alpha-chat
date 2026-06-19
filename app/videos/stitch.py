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

# Crossfade between consecutive segments to smooth the visual seam where independently-generated
# Seedance clips meet (the avatar's pose/look can shift at a hard cut). Kept short so it falls on the
# sentence-boundary pause between segments (the script is split on sentence boundaries).
SEGMENT_TRANSITION_SEC = 0.4


class StitchError(Exception):
    pass


def _crossfade_filtergraph(n: int, durs: list[float], t: float) -> str:
    """ffmpeg filter_complex that crossfades the VIDEO between segments (smooth seam) while HARD-
    joining the AUDIO (no overlapping/garbled speech). Each clip's audio stays head-aligned with its
    video; only the trailing `t` of every non-final clip (its fade-out region) is dropped, so the
    audio total equals the crossfaded video total and A/V stay in sync.

    Video: chained xfade. The offset of join i is the running output length so far minus t. Audio:
    each non-final clip trimmed to (dur - t), the last clip kept whole, then all concatenated."""
    vparts: list[str] = []
    prev = "[0:v]"
    cum = durs[0]
    for i in range(1, n):
        offset = max(0.0, cum - t)
        out = "[v]" if i == n - 1 else f"[vx{i}]"
        vparts.append(f"{prev}[{i}:v]xfade=transition=fade:duration={t:.3f}:offset={offset:.3f}{out}")
        cum = cum + durs[i] - t
        prev = out

    aparts: list[str] = []
    alabels: list[str] = []
    for i in range(n):
        lbl = f"[a{i}]"
        if i < n - 1:
            aparts.append(f"[{i}:a]atrim=end={max(0.0, durs[i] - t):.3f},asetpts=PTS-STARTPTS{lbl}")
        else:
            aparts.append(f"[{i}:a]asetpts=PTS-STARTPTS{lbl}")
        alabels.append(lbl)
    aparts.append(f"{''.join(alabels)}concat=n={n}:v=0:a=1[a]")
    return ";".join(vparts + aparts)


def _hardcut_filtergraph(n: int) -> str:
    """Plain back-to-back concat (no crossfade) — the resilient fallback if a crossfade render fails."""
    streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    return f"{streams}concat=n={n}:v=1:a=1[v][a]"


def _render(inputs: list[str], fc: str, out_path: "Path", cfg: "broll.BrollConfig") -> None:
    args = [cfg.ffmpeg, "-y", *inputs, "-filter_complex", fc,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out_path)]
    broll._run_ffmpeg(args, cfg.timeout_sec)


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
        # exact w×h×fps + a stereo AAC track so xfade/concat get identical stream params.
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

        n = len(norm_paths)
        durs = [broll._probe(str(p), cfg)[3] for p in norm_paths]
        if any(not d or d <= 0 for d in durs):
            raise StitchError("could not probe a segment duration")
        t = max(0.2, min(SEGMENT_TRANSITION_SEC, min(durs) / 4.0))
        inputs: list[str] = []
        for p in norm_paths:
            inputs += ["-i", str(p)]
        out_path = tmpp / "stitched.mp4"

        # Crossfade the segments; if that render fails for any reason, fall back to a plain hard cut
        # so a stitched video is still delivered.
        try:
            _render(inputs, _crossfade_filtergraph(n, durs, t), out_path, cfg)
        except broll.BrollError as e:
            log.warning("crossfade stitch failed (%s); falling back to a hard-cut concat", e)
            _render(inputs, _hardcut_filtergraph(n), out_path, cfg)

        data = out_path.read_bytes()
        if not data:
            raise StitchError("ffmpeg produced empty output")
        # Sanity log: crossfaded duration should be ~ sum(segments) - (n-1)*t. A large shortfall means
        # a normalize/stitch regression (e.g. the old -shortest duration bug).
        out_dur = broll._probe(str(out_path), cfg)[3]
        log.info("stitched %d clips -> %.1fs (segments %s, xfade %.2fs)",
                 n, out_dur, ", ".join(f"{d:.1f}s" for d in durs), t)
        if out_dur < 0.8 * (sum(durs) - (n - 1) * t):
            log.warning("stitched duration %.1fs is well under the expected %.1fs",
                        out_dur, sum(durs) - (n - 1) * t)
        return data


async def concat_clips(clips: list[bytes], *, cfg: Optional["broll.BrollConfig"] = None) -> bytes:
    """Join cinematic segment clips (in order) into one MP4. Raises StitchError on failure."""
    cfg = cfg or broll.BrollConfig.from_settings()
    async with broll._sem:   # share the b-roll/outro ffmpeg concurrency guard
        return await asyncio.to_thread(_concat_sync, clips, cfg)
