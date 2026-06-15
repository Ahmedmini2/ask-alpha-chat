"""Unit tests for the Hormozi ASS caption builder (pure — no ffmpeg/network). Locks the styling
contract and the per-word highlight timing; the actual burn is verified on a live render."""
from dataclasses import replace

from app.videos.captions import (
    CaptionConfig,
    build_hormozi_ass,
    _ass_color,
    _ass_time,
    _lines_of,
)

CFG = CaptionConfig()

WORDS = [
    {"text": "wake", "start": 0.0, "end": 0.4},
    {"text": "up", "start": 0.4, "end": 0.7},
    {"text": "to", "start": 0.7, "end": 0.9},
    {"text": "lagoon", "start": 0.95, "end": 1.5},
    {"text": "views", "start": 1.5, "end": 2.0},
]


def _dialogues(ass: str) -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]


def test_ass_color_conversion():
    assert _ass_color("#FFD60A") == "&H000AD6FF"   # yellow -> ASS BGR
    assert _ass_color("#FFFFFF") == "&H00FFFFFF"
    assert _ass_color("#000000") == "&H00000000"


def test_ass_time_format():
    assert _ass_time(1.5) == "0:00:01.50"
    assert _ass_time(75.2) == "0:01:15.20"
    assert _ass_time(0) == "0:00:00.00"


def test_lines_of_groups_by_n():
    assert _lines_of(WORDS, 3) == [WORDS[0:3], WORDS[3:5]]
    assert _lines_of(WORDS, 5) == [WORDS]


def test_doc_structure_and_playres():
    ass = build_hormozi_ass(WORDS, 1080, 1920, CFG)
    assert "[Script Info]" in ass and "[V4+ Styles]" in ass and "[Events]" in ass
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert f"Style: Hormozi,{CFG.font_name}," in ass


def test_one_dialogue_per_word_and_active_highlight():
    ass = build_hormozi_ass(WORDS, 1080, 1920, CFG)
    assert len(_dialogues(ass)) == len(WORDS)             # one event per word
    assert ass.count("&H000AD6FF") == len(WORDS)          # exactly one yellow active word each


def test_text_is_uppercased():
    ass = build_hormozi_ass(WORDS, 1080, 1920, CFG)
    assert "WAKE" in ass and "LAGOON" in ass
    assert "wake" not in ass and "lagoon" not in ass


def test_highlight_advances_within_line():
    # within a 3-word line, each word's event ends when the next word starts (line stays up)
    ass = build_hormozi_ass(WORDS, 1080, 1920, CFG)
    d = _dialogues(ass)
    assert d[0].split(",")[1] == "0:00:00.00" and d[0].split(",")[2] == "0:00:00.40"
    assert d[1].split(",")[1] == "0:00:00.40" and d[1].split(",")[2] == "0:00:00.70"
    # last word of the line ends at its own end, not a next-word start
    assert d[2].split(",")[2] == "0:00:00.90"


def test_words_per_line_config():
    ass = build_hormozi_ass(WORDS, 1080, 1920, replace(CFG, words_per_line=5))
    # one line of 5 -> still one Dialogue per word
    assert len(_dialogues(ass)) == 5


def test_pop_toggle():
    on = build_hormozi_ass(WORDS, 1080, 1920, replace(CFG, pop=True))
    off = build_hormozi_ass(WORDS, 1080, 1920, replace(CFG, pop=False))
    assert "\\t(" in on            # scale transform present when pop on
    assert "\\t(" not in off


def test_braces_in_text_are_stripped():
    words = [{"text": "he{l}lo", "start": 0.0, "end": 0.5}]
    ass = build_hormozi_ass(words, 1080, 1920, CFG)
    # only our own override braces remain — the stray text braces are gone
    assert "HE{L}LO" not in ass and "HELLO" in ass
