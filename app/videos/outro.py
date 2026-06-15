"""Append the Allegiance outro to a finished promo video with a short crossfade transition.

The poller calls `append_outro(main_mp4_bytes)` as the very last post-edit step (after b-roll and
captions). It auto-detects the video's orientation from its own dimensions, picks the matching outro
asset (portrait vs landscape), normalises both clips to the MAIN video's resolution/fps with a
guaranteed audio track, crossfades them over a short transition (xfade + acrossfade) and returns the
merged MP4. Reuses the b-roll module's ffmpeg path/semaphore/probe/run helpers.

Best-effort by contract: any failure raises OutroError and the caller delivers the un-outro'd video.
Requires ffmpeg + ffprobe on PATH (installed in the Dockerfile), like b-roll/captions.
"""
import asyncio
import logging
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from app.videos import broll

log = logging.getLogger("askalpha.outro")

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
# Orientation -> outro asset. Landscape (16:9) and portrait (9:16) are pre-rendered separately so
# the QR/branding never gets letterboxed or stretched.
PORTRAIT_OUTRO = _ASSET_DIR / "General QR A-Outro.mp4"
LANDSCAPE_OUTRO = _ASSET_DIR / "Landscape General QR A-Outro.mp4"

TRANSITION_SEC = 0.5   # the "tiny transition" — a quick crossfade between the promo and the outro


class OutroError(Exception):
    pass


def _outro_asset_for(width: int, height: int) -> Path:
    """Pick the outro that matches the finished video's orientation."""
    return LANDSCAPE_OUTRO if width >= height else PORTRAIT_OUTRO


def _transition_duration(main_dur: float, outro_dur: float) -> float:
    """The crossfade length: the target TRANSITION_SEC, but never more than a third of either clip
    (xfade needs both clips to be at least this long), and at least 0.2s so it stays visible."""
    return max(0.2, min(TRANSITION_SEC, main_dur / 3.0, outro_dur / 3.0))


def _has_audio(path: str, cfg: "broll.BrollConfig") -> bool:
    """True if the file has at least one audio stream (ffprobe). False on any probe issue."""
    try:
        out = subprocess.run(
            [cfg.ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and "audio" in (out.stdout or "")


def _normalize(src: str, dst: str, w: int, h: int, fps: float, cfg: "broll.BrollConfig") -> None:
    """Re-encode `src` to exactly w×h @ fps, yuv420p, with a stereo AAC track (real audio if the
    clip has one, otherwise synthesised silence) so both clips share identical params for xfade."""
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.3f},format=yuv420p")
    common_v = ["-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p"]
    common_a = ["-c:a", "aac", "-ar", "44100", "-ac", "2"]
    if _has_audio(src, cfg):
        # Pin the audio length to the video length: `apad` pads short audio with silence and
        # `-shortest` (video ends first) trims long audio, so audio_len == video_len exactly.
        # xfade pins the video transition to a fixed timestamp while acrossfade always anchors to
        # the FIRST input's audio length; if the two differed the outro audio would lead/lag its
        # video. Forcing audio==video here (for both clips) keeps the two transitions coincident.
        args = [cfg.ffmpeg, "-y", "-i", src, "-vf", vf, "-af", "apad",
                "-map", "0:v:0", "-map", "0:a:0", "-shortest", *common_v, *common_a,
                "-movflags", "+faststart", dst]
    else:
        # Synthesise a silent stereo track and trim it to the video length (-shortest).
        args = [cfg.ffmpeg, "-y", "-i", src,
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                *common_v, *common_a, "-movflags", "+faststart", dst]
    broll._run_ffmpeg(args, cfg.timeout_sec)


def _append_sync(main_bytes: bytes, cfg: "broll.BrollConfig") -> bytes:
    with TemporaryDirectory(prefix="outro-") as tmp:
        tmpp = Path(tmp)
        main_path = tmpp / "main.mp4"
        main_path.write_bytes(main_bytes)

        w, h, fps, _main_dur = broll._probe(str(main_path), cfg)
        asset = _outro_asset_for(w, h)
        if not asset.is_file():
            raise OutroError(f"outro asset missing: {asset.name}")

        main_n = tmpp / "main_n.mp4"
        outro_n = tmpp / "outro_n.mp4"
        _normalize(str(main_path), str(main_n), w, h, fps, cfg)
        _normalize(str(asset), str(outro_n), w, h, fps, cfg)

        # Derive the transition from the NORMALIZED clips: after _normalize each clip has
        # audio_len == video_len, so the xfade `offset` (= main_n duration - d) coincides with
        # acrossfade's implicit anchor (first input's audio length - d). Using the pre-normalize
        # probe instead would re-introduce skew when the original container duration (= the longer
        # of its streams) differed from the normalized clip's duration.
        _, _, _, main_dur = broll._probe(str(main_n), cfg)
        _, _, _, outro_dur = broll._probe(str(outro_n), cfg)

        # Keep the transition shorter than either clip; both must be at least `D` long for xfade.
        d = _transition_duration(main_dur, outro_dur)
        offset = max(0.0, main_dur - d)

        out_path = tmpp / "out.mp4"
        fc = (f"[0:v][1:v]xfade=transition=fade:duration={d:.3f}:offset={offset:.3f}[v];"
              f"[0:a][1:a]acrossfade=d={d:.3f}[a]")
        args = [cfg.ffmpeg, "-y", "-i", str(main_n), "-i", str(outro_n),
                "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                "-movflags", "+faststart", str(out_path)]
        broll._run_ffmpeg(args, cfg.timeout_sec)

        data = out_path.read_bytes()
        if not data:
            raise OutroError("ffmpeg produced empty output")
        return data


async def append_outro(source: "str | bytes", *, cfg: Optional["broll.BrollConfig"] = None) -> bytes:
    """Append the orientation-correct Allegiance outro (with a short crossfade) to the finished
    video and return the merged MP4 bytes. `source` is a URL (downloaded here) or raw bytes.
    Raises OutroError on failure so the caller can fall back to the un-outro'd video."""
    cfg = cfg or broll.BrollConfig.from_settings()
    if isinstance(source, (bytes, bytearray)):
        src = bytes(source)
    else:
        src = await broll._fetch_bytes(source)
        if not src:
            raise OutroError("could not download source video")
    async with broll._sem:   # share the b-roll ffmpeg concurrency guard
        return await asyncio.to_thread(_append_sync, src, cfg)
