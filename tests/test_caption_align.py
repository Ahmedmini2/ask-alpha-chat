"""Unit tests for forced alignment of the known script onto whisper word timings
(app/videos/align.py). All pure — no network."""
from app.videos.align import (
    _display,
    _norm,
    _tokenize_script,
    align_script_to_words,
)


def _w(text, start, end):
    return {"text": text, "start": start, "end": end}


def _texts(words):
    return [w["text"] for w in words]


# --------------------------------------------------------------------------- helpers


def test_norm_strips_punct_and_case():
    assert _norm("Marina,") == "marina"
    assert _norm("Damac") == "damac"
    assert _norm("1.4") == "14"
    assert _norm("—") == ""
    assert _norm("") == ""


def test_display_trims_edge_punct_keeps_internal():
    assert _display("Marina,") == "Marina"
    assert _display("1.4") == "1.4"
    assert _display('"Damac"') == "Damac"
    assert _display("co-op") == "co-op"  # internal hyphen kept; edges stripped
    assert _display("—") == "—"  # pure punctuation falls back to itself


def test_tokenize_drops_pure_punctuation():
    assert _tokenize_script("Damac Lagoons — a great buy") == \
        ["Damac", "Lagoons", "a", "great", "buy"]
    assert _tokenize_script("") == []
    assert _tokenize_script("   ") == []


# --------------------------------------------------------------------------- alignment core


def test_brand_misspelling_is_corrected_with_whisper_timing():
    # whisper mis-heard the brand; the script has the truth. Timing must be preserved.
    script = "Damac Lagoons is amazing"
    words = [_w("Damak", 0.0, 0.4), _w("Lagoons", 0.4, 0.9),
             _w("is", 0.9, 1.0), _w("amazing", 1.0, 1.6)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["Damac", "Lagoons", "is", "amazing"]
    assert out[0]["start"] == 0.0 and out[0]["end"] == 0.4
    assert out[3]["start"] == 1.0 and out[3]["end"] == 1.6


def test_multiple_brand_fixes():
    script = "Emaar Beachfront and Sobha Hartland"
    words = [_w("Amar", 0.0, 0.5), _w("Beachfront", 0.5, 1.0), _w("and", 1.0, 1.2),
             _w("Soba", 1.2, 1.6), _w("Hartland", 1.6, 2.0)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["Emaar", "Beachfront", "and", "Sobha", "Hartland"]


def test_exact_match_passthrough_uses_script_surface():
    script = "best value in dubai"
    words = [_w("best", 0.0, 0.3), _w("value", 0.3, 0.6),
             _w("in", 0.6, 0.7), _w("dubai", 0.7, 1.2)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["best", "value", "in", "dubai"]
    assert [(w["start"], w["end"]) for w in out] == \
        [(0.0, 0.3), (0.3, 0.6), (0.6, 0.7), (0.7, 1.2)]


def test_script_punctuation_stripped_in_caption():
    script = "Marina, Downtown, JVC."
    words = [_w("Marina", 0.0, 0.5), _w("Downtown", 0.5, 1.0), _w("JVC", 1.0, 1.5)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["Marina", "Downtown", "JVC"]


def test_unequal_replace_distributes_timing():
    # script "1.4" was spoken/heard as three words — one caption token spans all three timings.
    script = "priced at 1.4 million dirhams"
    words = [_w("priced", 0.0, 0.4), _w("at", 0.4, 0.6),
             _w("one", 0.6, 0.8), _w("point", 0.8, 1.0), _w("four", 1.0, 1.2),
             _w("million", 1.2, 1.7), _w("dirhams", 1.7, 2.2)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["priced", "at", "1.4", "million", "dirhams"]
    onefour = out[2]
    assert onefour["text"] == "1.4"
    assert onefour["start"] == 0.6 and onefour["end"] == 1.2  # spans one+point+four
    # timeline stays monotonic and ends where whisper ended
    assert out[-1]["end"] == 2.2


def test_whisper_insertion_is_kept():
    # whisper hallucinated/heard an extra "uh" not in the script -> keep it verbatim.
    script = "great location here"
    words = [_w("great", 0.0, 0.3), _w("uh", 0.3, 0.4),
             _w("location", 0.4, 0.9), _w("here", 0.9, 1.2)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["great", "uh", "location", "here"]


def test_script_only_word_is_dropped():
    # script has a word whisper never spoke -> no timing to give it, so it's dropped.
    script = "the absolutely best deal"
    words = [_w("the", 0.0, 0.2), _w("best", 0.2, 0.6), _w("deal", 0.6, 1.0)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["the", "best", "deal"]
    # every emitted word still carries a valid timing
    assert all(w["end"] >= w["start"] for w in out)


def test_brand_fix_survives_number_expansion_in_short_hook():
    # Regression: a short promo hook with BOTH a brand respelling and a price written as
    # shorthand ("AED 1.4M" -> spoken "one point four million dirhams"). difflib's raw .ratio()
    # is ~0.40 here (the number expansion inflates the denominator), which used to trip the
    # global gate and silently fall back to whisper — burning in the misspelled "Damak". The
    # gate must measure 1:1-comparable coverage instead, so the brand fix survives.
    script = "Damac Bay priced from AED 1.4M"
    words = [_w("Damak", 0.0, 0.3), _w("Bay", 0.3, 0.6), _w("priced", 0.6, 0.9),
             _w("from", 0.9, 1.2), _w("one", 1.2, 1.5), _w("point", 1.5, 1.8),
             _w("four", 1.8, 2.1), _w("million", 2.1, 2.4), _w("dirhams", 2.4, 2.7)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["Damac", "Bay", "priced", "from", "AED", "1.4M"]
    assert out[0]["text"] == "Damac"  # brand fix applied, not the misheard "Damak"
    # the price block ("AED 1.4M", 2 script tokens) is distributed over the 5 spoken words' span
    assert out[-1]["end"] == 2.7  # timeline still ends where whisper ended
    assert all(w["end"] >= w["start"] for w in out)


def test_multiple_brand_fixes_with_number_expansion():
    script = "Sobha Hartland from AED 1.4M"
    words = [_w("Soba", 0.0, 0.3), _w("Hartland", 0.3, 0.6), _w("from", 0.6, 0.9),
             _w("one", 0.9, 1.2), _w("point", 1.2, 1.5), _w("four", 1.5, 1.8),
             _w("million", 1.8, 2.1), _w("dirhams", 2.1, 2.4)]
    out = align_script_to_words(script, words)
    assert _texts(out)[:3] == ["Sobha", "Hartland", "from"]
    assert "Soba" not in _texts(out)


def test_all_expansion_no_anchor_falls_back():
    # A wholly-wrong multi-token script that difflib renders as a single unequal replace into a
    # short clip has NO 1:1 anchor confirming it belongs to the audio -> must fall back to whisper
    # (not optimistically force the script just because the span is "expansion-shaped").
    script = "completely unrelated sentence about cats and dogs"
    words = [_w("buy", 0.0, 0.3), _w("this", 0.3, 0.6), _w("villa", 0.6, 1.0)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["buy", "this", "villa"]


def test_low_ratio_falls_back_to_whisper():
    # script bears no relation to the audio -> trust whisper's own transcription.
    script = "completely unrelated sentence about cats and dogs"
    words = [_w("buy", 0.0, 0.3), _w("this", 0.3, 0.6), _w("villa", 0.6, 1.0)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["buy", "this", "villa"]


def test_empty_script_returns_whisper_words():
    words = [_w("Damak", 0.0, 0.5), _w("Lagoons", 0.5, 1.0)]
    assert _texts(align_script_to_words("", words)) == ["Damak", "Lagoons"]
    assert _texts(align_script_to_words(None, words)) == ["Damak", "Lagoons"]


def test_empty_words_returns_empty():
    assert align_script_to_words("anything at all", []) == []


def test_output_shape_and_types():
    script = "Damac Lagoons"
    words = [_w("Damak", 0.0, 0.5), _w("Lagoons", 0.5, 1.0)]
    out = align_script_to_words(script, words)
    for w in out:
        assert set(w.keys()) == {"text", "start", "end"}
        assert isinstance(w["start"], float) and isinstance(w["end"], float)
        assert w["end"] >= w["start"]


def test_blank_whisper_text_entries_are_ignored():
    script = "Damac Lagoons"
    words = [_w("Damak", 0.0, 0.5), _w("   ", 0.5, 0.6), _w("Lagoons", 0.6, 1.0)]
    out = align_script_to_words(script, words)
    assert _texts(out) == ["Damac", "Lagoons"]
