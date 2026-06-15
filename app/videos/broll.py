"""Full-frame b-roll cutaways for promo videos.

After HeyGen renders the avatar video, we cut full-screen Ken-Burns (slow pan/zoom) stills of
the property into the MIDDLE of the clip — the avatar holds the opening hook and the closing
CTA — while the original narration audio plays continuously underneath. The composite is then
captioned by Descript, so captions overlay everything.

Everything here is best-effort: `add_broll` returns None when there's nothing worth doing
(short video / no usable stills) and raises BrollError on a real failure; the caller
(heygen_poller) falls back to captioning the raw HeyGen video either way. The planners are pure
and DETERMINISTIC (no random/time — Ken-Burns motion is chosen from a seed), so a poller retry
reproduces the same edit.

Requires ffmpeg + ffprobe on PATH (installed in the Dockerfile).

ffmpeg note: still inputs are added WITHOUT `-loop` (a single image frame). zoompan with a
single input frame emits exactly `d` frames — looping the input instead would multiply frames.
Verified-recipe; see the live-run check in the b-roll plan.
"""
import asyncio
import io
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

import httpx

from app.config import settings
from app.integrations import bedrock_images

log = logging.getLogger("askalpha.broll")


class BrollError(Exception):
    pass


@dataclass(frozen=True)
class BrollConfig:
    max_clips: int = 5
    head_ratio: float = 0.25
    tail_ratio: float = 0.25
    head_min: float = 3.0
    head_max: float = 12.0
    tail_min: float = 3.0
    tail_max: float = 10.0
    min_total_dur: float = 12.0
    target_segment: float = 4.0
    min_segment: float = 2.5
    max_segment: float = 7.0
    crf: int = 20
    preset: str = "veryfast"
    zoom_max: float = 1.18
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout_sec: int = 300

    @classmethod
    def from_settings(cls) -> "BrollConfig":
        s = settings
        return cls(
            max_clips=s.broll_max_clips,
            head_ratio=s.broll_head_ratio, tail_ratio=s.broll_tail_ratio,
            head_min=s.broll_head_min_sec, head_max=s.broll_head_max_sec,
            tail_min=s.broll_tail_min_sec, tail_max=s.broll_tail_max_sec,
            min_total_dur=s.broll_min_total_dur_sec,
            target_segment=s.broll_target_segment_sec,
            min_segment=s.broll_min_segment_sec, max_segment=s.broll_max_segment_sec,
            crf=s.broll_crf, preset=s.broll_preset, zoom_max=s.broll_zoom_max,
            ffmpeg=s.ffmpeg_path, ffprobe=s.ffprobe_path, timeout_sec=s.broll_ffmpeg_timeout_sec,
        )


@dataclass(frozen=True)
class Segment:
    kind: str                       # "avatar" | "broll"
    start: float
    end: float
    image_index: Optional[int] = None


# Ken-Burns motions, picked deterministically per clip from the seed.
_KB_PATTERNS = ("zoom_in", "zoom_out", "pan_right", "pan_left", "pan_up", "pan_down")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _merge_adjacent_avatar(segs: list[Segment]) -> list[Segment]:
    """Collapse back-to-back avatar segments into one (cosmetic; fewer filter nodes)."""
    out: list[Segment] = []
    for s in segs:
        if out and out[-1].kind == "avatar" and s.kind == "avatar":
            prev = out[-1]
            out[-1] = Segment("avatar", prev.start, s.end)
        else:
            out.append(s)
    return out


# --------------------------------------------------------------------------- planners (pure)

def plan_segments(dur: float, n_images: int, cfg: BrollConfig) -> list[Segment]:
    """Lay out the timeline: avatar holds the head (hook) and tail (CTA); b-roll stills fill the
    middle. Returns a contiguous, gap-free list covering [0, dur] whose first and last segments
    are avatar. A list with NO 'broll' segment means "don't edit" (short video / no images)."""
    if dur < cfg.min_total_dur or n_images < 1:
        return [Segment("avatar", 0.0, dur)]

    head = _clamp(dur * cfg.head_ratio, cfg.head_min, cfg.head_max)
    tail = _clamp(dur * cfg.tail_ratio, cfg.tail_min, cfg.tail_max)
    middle = dur - head - tail
    if middle < cfg.min_segment:
        return [Segment("avatar", 0.0, dur)]

    k = int(middle // cfg.target_segment)
    k = max(1, min(k, n_images, cfg.max_clips))

    even = middle / k
    if even <= cfg.max_segment:
        broll_len, gap = even, 0.0
    else:
        broll_len = cfg.max_segment
        gap = (middle - broll_len * k) / k          # avatar "breather" after each b-roll clip
        if gap < cfg.min_segment:                   # too small to be worth a cut — let clips run
            broll_len, gap = even, 0.0

    segs: list[Segment] = [Segment("avatar", 0.0, head)]
    t = head
    for i in range(k):
        segs.append(Segment("broll", t, t + broll_len, i))
        t += broll_len
        if gap > 1e-6 and i < k - 1:                 # no trailing breather (folds into the tail)
            segs.append(Segment("avatar", t, t + gap))
            t += gap
    # Close out exactly at dur with the tail avatar (absorbs any float remainder).
    segs.append(Segment("avatar", t, dur))
    return _merge_adjacent_avatar(segs)


def plan_image_sources(available_photos: int, k_target: int, cfg: BrollConfig) -> tuple[int, int]:
    """How many project photos to use vs Bedrock stills to generate. Photos first; AI tops up."""
    k = max(1, min(k_target, cfg.max_clips))
    use_photos = min(max(0, available_photos), k)
    generate_ai = max(0, k - use_photos)
    return use_photos, generate_ai


# --------------------------------------------------------------------------- ffmpeg helpers

def _parse_fps(rate: Optional[str]) -> float:
    try:
        if rate and "/" in rate:
            num, den = rate.split("/")
            den_f = float(den)
            if den_f:
                return float(num) / den_f
        if rate:
            return float(rate)
    except (ValueError, ZeroDivisionError):
        pass
    return 30.0


def _probe(path: str, cfg: BrollConfig) -> tuple[int, int, float, float]:
    """Return (width, height, fps, duration) for the source video via ffprobe."""
    try:
        out = subprocess.run(
            [cfg.ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BrollError(f"ffprobe error: {e}")
    if out.returncode != 0:
        raise BrollError(f"ffprobe failed: {out.stderr[:300]}")
    data = json.loads(out.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    fps = _parse_fps(st.get("r_frame_rate"))
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    if not (w and h and dur > 0):
        raise BrollError(f"ffprobe missing dimensions/duration: {out.stdout[:200]}")
    return w, h, fps, dur


def _kenburns_chain(in_label: str, out_label: str, w: int, h: int, nframes: int,
                    fps: float, pattern: str, zoom_max: float) -> str:
    """One still -> a WxH Ken-Burns clip of `nframes` frames. Cover-scales to 2x (crisper pan),
    then zoompan. x/y are in the (2w x 2h) input space; iw/zoom is the visible width."""
    zr = zoom_max - 1.0
    if pattern == "zoom_in":
        z = f"min(1+{zr:.4f}*on/{nframes},{zoom_max})"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif pattern == "zoom_out":
        z = f"max({zoom_max}-{zr:.4f}*on/{nframes},1)"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif pattern == "pan_right":
        z = f"{zoom_max}"
        x, y = f"(iw-iw/zoom)*on/{nframes}", "(ih-ih/zoom)/2"
    elif pattern == "pan_left":
        z = f"{zoom_max}"
        x, y = f"(iw-iw/zoom)*(1-on/{nframes})", "(ih-ih/zoom)/2"
    elif pattern == "pan_up":
        z = f"{zoom_max}"
        x, y = "(iw-iw/zoom)/2", f"(ih-ih/zoom)*(1-on/{nframes})"
    else:  # pan_down
        z = f"{zoom_max}"
        x, y = "(iw-iw/zoom)/2", f"(ih-ih/zoom)*on/{nframes}"
    return (
        f"{in_label}scale={2*w}:{2*h}:force_original_aspect_ratio=increase,crop={2*w}:{2*h},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={nframes}:s={w}x{h}:fps={fps:.5f},"
        f"format=yuv420p,setsar=1{out_label}"
    )


def _build_filter_complex(segments: list[Segment], w: int, h: int, fps: float,
                          cfg: BrollConfig, seed: int) -> tuple[str, list[int]]:
    """Build the single filter_complex (avatar trims + Ken-Burns stills -> concat) and the list
    of image indices in still-input order (input N+1 is the N-th b-roll still)."""
    parts: list[str] = []
    labels: list[str] = []
    broll_order: list[int] = []
    for si, seg in enumerate(segments):
        if seg.kind == "avatar":
            lbl = f"v{si}"
            parts.append(
                f"[0:v]trim=start={seg.start:.3f}:end={seg.end:.3f},setpts=PTS-STARTPTS,"
                f"fps={fps:.5f},scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},format=yuv420p,setsar=1[{lbl}]"
            )
            labels.append(lbl)
        else:
            input_idx = 1 + len(broll_order)        # still inputs are 1..k, in order
            nframes = max(1, round((seg.end - seg.start) * fps))
            pattern = _KB_PATTERNS[(seed + (seg.image_index or 0)) % len(_KB_PATTERNS)]
            lbl = f"v{si}"
            parts.append(_kenburns_chain(f"[{input_idx}:v]", f"[{lbl}]", w, h, nframes,
                                         fps, pattern, cfg.zoom_max))
            labels.append(lbl)
            broll_order.append(seg.image_index if seg.image_index is not None else 0)
    concat_in = "".join(f"[{l}]" for l in labels)
    parts.append(f"{concat_in}concat=n={len(labels)}:v=1:a=0[vout]")
    return ";".join(parts), broll_order


def _run_ffmpeg(args: list[str], timeout: int) -> None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BrollError(f"ffmpeg timed out after {timeout}s")
    except OSError as e:
        raise BrollError(f"ffmpeg not runnable: {e}")
    if out.returncode != 0:
        raise BrollError(f"ffmpeg failed ({out.returncode}): {out.stderr[-500:]}")


def _normalize_still(blob: bytes) -> Optional[bytes]:
    """Bake EXIF rotation and re-encode to clean JPEG so ffmpeg frames aren't sideways."""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(blob))
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception as e:  # pragma: no cover — corrupt/unsupported image
        log.warning("broll: still normalize failed: %s", e)
        return None


async def _fetch_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(url)
            if r.status_code < 400:
                return r.content
            log.warning("broll: source fetch %s -> %s", url[:60], r.status_code)
    except Exception as e:  # pragma: no cover
        log.warning("broll: source fetch error: %s", e)
    return None


# Bound concurrent ffmpeg encodes (CPU-heavy) — mirrors the poller's caption semaphore.
_sem = asyncio.Semaphore(max(1, settings.broll_concurrency))


def _render_sync(src_bytes: bytes, blobs: list[bytes], seed: int, cfg: BrollConfig) -> Optional[bytes]:
    with TemporaryDirectory(prefix="broll-") as tmp:
        tmpp = Path(tmp)
        src_path = tmpp / "source.mp4"
        src_path.write_bytes(src_bytes)
        w, h, fps, dur = _probe(str(src_path), cfg)

        segments = plan_segments(dur, len(blobs), cfg)
        broll_segs = [s for s in segments if s.kind == "broll"]
        if not broll_segs:
            return None  # nothing worth editing — caller keeps the raw video

        filter_str, broll_order = _build_filter_complex(segments, w, h, fps, cfg, seed)

        inputs = ["-i", str(src_path)]
        for j, img_idx in enumerate(broll_order):
            still = tmpp / f"still_{j}.jpg"
            still.write_bytes(blobs[img_idx])
            inputs += ["-i", str(still)]            # NO -loop: single frame -> zoompan emits d frames

        out_path = tmpp / "out.mp4"
        args = [
            cfg.ffmpeg, "-y", *inputs,
            "-filter_complex", filter_str,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-shortest", str(out_path),
        ]
        _run_ffmpeg(args, cfg.timeout_sec)
        data = out_path.read_bytes()
        if not data:
            raise BrollError("ffmpeg produced empty output")
        return data


async def detect_aspect(source_bytes: bytes, cfg: Optional[BrollConfig] = None) -> str:
    """Cheap probe of already-downloaded video bytes -> '16:9' (landscape) or '9:16'. Used to
    pick the AI-filler generation aspect; the final composite always uses the probed pixels."""
    cfg = cfg or BrollConfig.from_settings()

    def _f() -> str:
        with TemporaryDirectory(prefix="broll-probe-") as tmp:
            p = Path(tmp) / "s.mp4"
            p.write_bytes(source_bytes)
            w, h, _, _ = _probe(str(p), cfg)
            return "16:9" if w > h else "9:16"

    try:
        return await asyncio.to_thread(_f)
    except BrollError:
        return "9:16"


async def add_broll(
    source: "str | bytes",
    image_blobs: list[bytes],
    aspect_ratio: str = "9:16",
    *,
    seed: int = 0,
    cfg: Optional[BrollConfig] = None,
) -> Optional[bytes]:
    """Cut the b-roll stills into the middle of the HeyGen video and return the composited MP4
    bytes. `source` may be the video URL (downloaded here) or pre-downloaded bytes. Returns None
    when there's nothing worth doing (short video / no usable stills); raises BrollError on a
    real failure. `aspect_ratio` is not used for pixel sizing (dimensions come from the probed
    source) — kept for symmetry with the caller."""
    cfg = cfg or BrollConfig.from_settings()
    if not image_blobs:
        return None
    if isinstance(source, (bytes, bytearray)):
        src = bytes(source)
    else:
        src = await _fetch_bytes(source)
        if not src:
            raise BrollError("could not download source video")
    async with _sem:
        return await asyncio.to_thread(_render_sync, src, list(image_blobs), seed, cfg)


# --------------------------------------------------------------------------- image sourcing

# Rotating scene foci so AI-filler stills differ from each other (deterministic by index).
_BROLL_FOCI = (
    "the building exterior at golden hour",
    "a resort-style swimming pool and sun deck",
    "an elegant lobby interior",
    "landscaped gardens and walking paths",
    "a skyline view from a high balcony",
    "a modern fitted kitchen and living space",
)


def _broll_prompt(project, focus: str, aspect_ratio: str) -> str:
    """Text-to-image prompt for an AI b-roll still — a full-frame SCENE (no presenter, unlike the
    background plate). Leads with the project's location/setting, no people."""
    loc = " ".join((getattr(project, "district", None) or "").split()
                   + (getattr(project, "city", None) or "").split())
    where = f"in {loc}" if loc else "in Dubai"
    composition = "horizontal 16:9 framing" if aspect_ratio == "16:9" else "vertical 9:16 framing"
    return (
        f"Cinematic real-estate b-roll of {focus} at the {project.name} development {where} — "
        "premium contemporary architecture, photorealistic, shallow depth of field, golden-hour "
        f"lighting, {composition}, no people, no text"
    )[:1400]


async def gather_broll_images(
    db, project, k_target: int, aspect_ratio: str, *, seed: int = 0,
    cfg: Optional[BrollConfig] = None,
) -> list[bytes]:
    """Collect up to ~k_target b-roll stills: the project's own photos first (by position),
    topped up with Bedrock-generated stills. Best-effort — returns 0..k normalized JPEG blobs
    and never raises. An empty list means the caller should skip b-roll."""
    cfg = cfg or BrollConfig.from_settings()
    from app.brochures import data as brochure_data, storage  # lazy: avoid import cycles

    try:
        images, _plans = await brochure_data._gather_assets(db, project)
    except Exception as e:
        log.warning("broll: asset gather failed: %s", e)
        images = []

    use_photos, _ = plan_image_sources(len(images), k_target, cfg)
    blobs: list[bytes] = []
    for a in images[:use_photos]:
        raw = await storage.fetch_asset_bytes(a.s3_bucket, a.s3_key)
        if not raw:
            continue
        norm = _normalize_still(raw)
        if norm:
            blobs.append(norm)

    need_ai = max(0, min(k_target, cfg.max_clips) - len(blobs))
    if need_ai:
        prompts = [_broll_prompt(project, _BROLL_FOCI[(seed + i) % len(_BROLL_FOCI)], aspect_ratio)
                   for i in range(need_ai)]
        results = await asyncio.gather(
            *[bedrock_images.generate_background_png(p, aspect_ratio=aspect_ratio) for p in prompts],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, (bytes, bytearray)):
                norm = _normalize_still(bytes(r))
                if norm:
                    blobs.append(norm)
            else:
                log.warning("broll: AI still failed: %s", r)
    return blobs
