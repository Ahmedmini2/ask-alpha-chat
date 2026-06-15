"""Hormozi-style burned-in captions via ffmpeg/libass (ASS subtitles).

Word timings come from FAL whisper (app/integrations/fal.py); here we turn them into an ASS
subtitle file styled like Hormozi captions — heavy uppercase font, a few words per line, the
spoken word highlighted yellow, thick black outline, lower-third — and burn it onto the video
with ffmpeg. This replaces the old prompt-driven Descript captions.

`build_hormozi_ass` is pure (unit-tested). `burn_hormozi` shells out to ffmpeg, reusing the
b-roll module's download / semaphore / probe / run helpers.
"""
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from app.config import settings
from app.videos import broll

log = logging.getLogger("askalpha.captions")


class CaptionError(Exception):
    pass


@dataclass(frozen=True)
class CaptionConfig:
    words_per_line: int = 3
    active_color: str = "#FFD60A"
    pop: bool = True
    font_name: str = "Anton"
    font_dir: str = "app/videos/assets/fonts"
    # ffmpeg knobs reused from the b-roll settings so there's one place to tune the encoder.
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    preset: str = "veryfast"
    crf: int = 20
    timeout_sec: int = 300

    @classmethod
    def from_settings(cls) -> "CaptionConfig":
        s = settings
        return cls(
            words_per_line=s.caption_words_per_line, active_color=s.caption_active_color,
            pop=s.caption_pop, font_name=s.caption_font_name, font_dir=s.caption_font_dir,
            ffmpeg=s.ffmpeg_path, ffprobe=s.ffprobe_path, preset=s.broll_preset,
            crf=s.broll_crf, timeout_sec=s.broll_ffmpeg_timeout_sec,
        )


# --------------------------------------------------------------------------- pure ASS builder

def _ass_color(hex_color: str) -> str:
    """'#RRGGBB' -> ASS '&H00BBGGRR' (opaque)."""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    """Seconds -> ASS 'H:MM:SS.cc' (centiseconds)."""
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _clean(text: str) -> str:
    """Strip braces/newlines that would break ASS override syntax; collapse whitespace."""
    return re.sub(r"\s+", " ", (text or "").replace("{", "").replace("}", "")).strip()


def _lines_of(words: list[dict], n: int) -> list[list[dict]]:
    n = max(1, n)
    return [words[i:i + n] for i in range(0, len(words), n)]


def _render_line(tokens: list[str], active: int, active_c: str, white_c: str, pop: bool) -> str:
    parts: list[str] = []
    for j, tok in enumerate(tokens):
        if j == active:
            if pop:
                parts.append(
                    f"{{\\c{active_c}\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}}{tok}"
                    f"{{\\c{white_c}\\fscx100\\fscy100}}"
                )
            else:
                parts.append(f"{{\\c{active_c}}}{tok}{{\\c{white_c}}}")
        else:
            parts.append(tok)
    return " ".join(parts)


def build_hormozi_ass(words: list[dict], width: int, height: int, cfg: CaptionConfig) -> str:
    """Render an ASS subtitle document: words grouped into short uppercase lines, the spoken word
    highlighted. Each word gets a Dialogue event spanning until the next word starts, so the line
    stays on screen and the highlight advances. Pure & deterministic."""
    fs = max(12, round(height * 0.052))
    outline = max(1, round(fs * 0.06))
    marginv = round(height * 0.16)
    white = _ass_color("#FFFFFF")
    active = _ass_color(cfg.active_color)

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
         "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
         "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: Hormozi,{cfg.font_name},{fs},{white},{white},&H00000000,&H00000000,"
         f"-1,0,0,0,100,100,0,0,1,{outline},2,2,40,40,{marginv},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    for line in _lines_of(words, cfg.words_per_line):
        tokens = [_clean(w["text"]).upper() for w in line]
        for j, w in enumerate(line):
            start = float(w["start"])
            # keep the line up across intra-line pauses: end at the next word's start
            end = float(line[j + 1]["start"]) if j + 1 < len(line) else float(w["end"])
            if end <= start:
                end = float(w["end"])
            text = _render_line(tokens, j, active, white, cfg.pop)
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Hormozi,,0,0,0,,{text}"
            )
    return "\n".join(header + events) + "\n"


# --------------------------------------------------------------------------- ffmpeg burn

def _ff_escape(path: str) -> str:
    """Escape a path for use inside the ffmpeg `subtitles=` filter argument."""
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _burn_sync(src_bytes: bytes, words: list[dict], width: Optional[int],
               height: Optional[int], cfg: CaptionConfig) -> bytes:
    with TemporaryDirectory(prefix="caption-") as tmp:
        tmpp = Path(tmp)
        src_path = tmpp / "in.mp4"
        src_path.write_bytes(src_bytes)
        if not (width and height):
            w, h, _, _ = broll._probe(str(src_path), broll.BrollConfig.from_settings())
        else:
            w, h = width, height

        ass_path = tmpp / "cap.ass"
        ass_path.write_text(build_hormozi_ass(words, w, h, cfg), encoding="utf-8")

        fontsdir = os.path.abspath(cfg.font_dir)
        vf = f"subtitles={_ff_escape(str(ass_path))}:fontsdir={_ff_escape(fontsdir)}"
        out_path = tmpp / "out.mp4"
        args = [
            cfg.ffmpeg, "-y", "-i", str(src_path), "-vf", vf,
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out_path),
        ]
        broll._run_ffmpeg(args, cfg.timeout_sec)
        data = out_path.read_bytes()
        if not data:
            raise CaptionError("ffmpeg produced empty output")
        return data


async def burn_hormozi(
    source: "str | bytes",
    words: list[dict],
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    cfg: Optional[CaptionConfig] = None,
) -> bytes:
    """Burn Hormozi captions onto the video and return the new MP4 bytes. `source` is a URL
    (downloaded here) or pre-downloaded bytes. Raises CaptionError on failure."""
    cfg = cfg or CaptionConfig.from_settings()
    if not words:
        raise CaptionError("no word timings")
    if isinstance(source, (bytes, bytearray)):
        src = bytes(source)
    else:
        src = await broll._fetch_bytes(source)
        if not src:
            raise CaptionError("could not download source video")
    async with broll._sem:   # share the b-roll ffmpeg concurrency guard
        return await asyncio.to_thread(_burn_sync, src, words, width, height, cfg)
