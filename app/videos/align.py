"""Align the known script text to FAL/whisper word timings (forced alignment).

HeyGen speaks our stored `videos.script` verbatim, but the caption pipeline used to burn in
whisper's *transcription* of the audio — which mis-spells proper nouns ("Damac" -> "Damak",
"Emaar" -> "Amar", …). We already have the ground-truth text, so we keep whisper's per-word
*timings* and substitute the *script's* spelling.

`align_script_to_words(script, words)` is pure & unit-tested. It tokenises both streams, diffs
them with difflib, and rebuilds the word list using script surface forms over whisper timestamps:

  - equal / equal-count replace -> 1:1, script text + the matching whisper word's timing
    (this is what fixes the brand-name spelling).
  - unequal replace -> the N script tokens share the M whisper words' combined time span
    (handles e.g. script "1.4" vs spoken "one point four").
  - delete (script-only, never spoken) -> dropped (no timing to give it).
  - insert (whisper heard something not in the script) -> kept verbatim (text + timing).

If the script and the audio don't correspond (low diff ratio) or anything is missing, it falls
back to the raw whisper words so captions are never worse than before.
"""
import difflib
import re
from typing import Optional

# Tokens whose normalised form is empty (pure punctuation) are ignored for alignment; we strip
# these from the *displayed* caption word too so the burn-in stays Hormozi-clean.
_EDGE_PUNCT = "\"'`.,!?;:()[]{}…—–-"
_NORM_RE = re.compile(r"[^a-z0-9]+")

# Below this coverage fraction we assume the stored script doesn't match the spoken audio (wrong
# script, failed TTS, …) and trust whisper's own transcription instead of forcing the script on it.
# NB: this is computed over the 1:1-comparable region only (see _aligned_coverage) — NOT
# difflib's raw .ratio(), which penalises legitimate number expansions ("1.4M" -> "one point four
# million dirhams") so hard that a short brand+price hook would fall back to whisper and silently
# drop the brand-spelling fix that is this module's whole reason for existing.
_MIN_RATIO = 0.5


def _norm(tok: str) -> str:
    """Comparison key: lowercase, drop everything but [a-z0-9] ('Marina,' -> 'marina', '1.4' -> '14')."""
    return _NORM_RE.sub("", (tok or "").lower())


def _display(tok: str) -> str:
    """Caption surface form: trim surrounding punctuation but keep internal ('Marina,' -> 'Marina',
    '1.4' -> '1.4'). Falls back to the raw token if stripping would empty it."""
    d = (tok or "").strip(_EDGE_PUNCT)
    return d or (tok or "").strip()


def _tokenize_script(script: str) -> list[str]:
    """Whitespace-split the script into surface tokens, keeping only those with a non-empty
    normalised form (so stray dashes/quotes don't desync the diff)."""
    return [t for t in (script or "").split() if _norm(t)]


def _word(text: str, start: float, end: float) -> dict:
    try:
        start = float(start)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(end)
    except (TypeError, ValueError):
        end = start
    if end < start:
        end = start
    return {"text": text, "start": start, "end": end}


def _distribute(script_toks: list[str], wwords: list[dict]) -> list[dict]:
    """Spread N script tokens evenly across the combined time span of M whisper words."""
    disp = [_display(t) for t in script_toks]
    disp = [d for d in disp if d]
    if not disp:
        return [dict(w) for w in wwords]
    start = float(wwords[0]["start"])
    end = float(wwords[-1]["end"])
    if end < start:
        end = start
    n = len(disp)
    span = (end - start) / n if n else 0.0
    out: list[dict] = []
    for k, d in enumerate(disp):
        s = start + k * span
        e = end if k == n - 1 else start + (k + 1) * span
        out.append(_word(d, s, e))
    return out


def _aligned_coverage(opcodes) -> float:
    """Fraction of the *1:1-comparable* script tokens that whisper actually matched.

    Why not difflib's raw .ratio()? A correct script diverges from the transcription in exactly
    two benign ways: brand respellings ("Damac"/"Damak" — a 1:1 replace) and number/currency
    expansions ("1.4M" -> 4-6 spoken words — an N!=M replace). .ratio() = 2*matches/(len_s+len_w)
    counts every expanded spoken word against the script, so a short hook like "Damac Bay from AED
    1.4M" scores ~0.4 and falls back to whisper — discarding the brand fix. Instead we measure
    similarity only over the region where script and audio are 1:1-comparable:

      numerator   = tokens in `equal` blocks (matched 1:1, e.g. "Bay", "from")
      denominator = `equal` + 1:1 `replace` (brand fixes / true mismatches) + `delete`
                    (script words never spoken). Unequal `replace` (number expansion) and `insert`
                    (whisper-only words) are EXCLUDED — they're neutral, not evidence either way.

    A genuinely-wrong script has few/no equal anchors -> low coverage -> fall back to whisper.
    A correct hook keeps its non-brand, non-number words as equal anchors -> high coverage."""
    eq = comparable = 0
    for tag, i1, i2, j1, j2 in opcodes:
        n, m = i2 - i1, j2 - j1
        if tag == "equal":
            eq += n
            comparable += n
        elif tag == "replace" and n == m:
            comparable += n      # 1:1 replace: comparable, but did not match
        elif tag == "delete":
            comparable += n      # script word never spoken: counts against coverage
        # unequal replace (number expansion) and insert (whisper-only): neutral, excluded.
    if eq == 0:
        # Not a single word matched 1:1. Even if the whole script is one unequal-replace span
        # (which _could_ be a number expansion), there's no anchor confirming the script belongs
        # to this audio — e.g. a wholly-wrong 7-token script "expanding" into a 3-word clip. With
        # zero evidence, don't force the script; fall back to whisper.
        return 0.0
    if comparable == 0:
        # There ARE equal anchors but no 1:1-comparable extras (the rest is pure number
        # expansion / inserts). The anchors confirm the script — trust it.
        return 1.0
    return eq / comparable


def align_script_to_words(script: Optional[str], words: list[dict]) -> list[dict]:
    """Return caption word dicts [{text,start,end}] using the SCRIPT's spelling over whisper's
    timings. Pure & deterministic. Never raises on ordinary input; degrades to the raw whisper
    words whenever it can't confidently align."""
    if not words:
        return []
    raw = [_word((w.get("text") or "").strip(), w.get("start", 0.0), w.get("end", 0.0))
           for w in words if (w.get("text") or "").strip()]
    if not raw:
        return [dict(w) for w in words]

    script_tokens = _tokenize_script(script or "")
    if not script_tokens:
        return raw

    s_norm = [_norm(t) for t in script_tokens]
    w_norm = [_norm(w["text"]) for w in raw]
    sm = difflib.SequenceMatcher(a=s_norm, b=w_norm, autojunk=False)
    opcodes = sm.get_opcodes()
    if _aligned_coverage(opcodes) < _MIN_RATIO:
        # Script and audio don't correspond — don't force a bad script onto good timings.
        return raw

    out: list[dict] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(j2 - j1):
                out.append(_word(_display(script_tokens[i1 + k]),
                                 raw[j1 + k]["start"], raw[j1 + k]["end"]))
        elif tag == "replace":
            n, m = i2 - i1, j2 - j1
            if n == m:
                for k in range(m):
                    out.append(_word(_display(script_tokens[i1 + k]),
                                     raw[j1 + k]["start"], raw[j1 + k]["end"]))
            else:
                out.extend(_distribute(script_tokens[i1:i2], raw[j1:j2]))
        elif tag == "insert":
            for k in range(j1, j2):
                out.append(dict(raw[k]))
        # tag == "delete": script tokens with no spoken audio -> nothing to emit.

    return out or raw
