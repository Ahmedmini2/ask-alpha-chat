"""Unit tests for chat-history budgeting (app/core/orchestrator._trim_history)."""
from types import SimpleNamespace

from app.core.orchestrator import _trim_history


def _rows(*pairs):
    """pairs are (role, content) OLDEST-first; return them NEWEST-first as _trim_history expects."""
    return [SimpleNamespace(role=r, content=c) for r, c in reversed(pairs)]


def _roles(out):
    return [m["role"] for m in out]


def _texts(out):
    return [m["content"][0]["text"] for m in out]


def test_keeps_all_when_under_budget_and_oldest_first():
    rows = _rows(("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "d"))
    out = _trim_history(rows, char_budget=10_000)
    assert _roles(out) == ["user", "assistant", "user", "assistant"]
    assert _texts(out) == ["a", "b", "c", "d"]   # oldest-first order preserved


def test_trims_oldest_when_over_budget():
    # 4 turns of 100 chars each; budget 250 keeps only the newest that fit.
    rows = _rows(("user", "x" * 100), ("assistant", "y" * 100),
                 ("user", "z" * 100), ("assistant", "w" * 100))
    out = _trim_history(rows, char_budget=250)
    # newest two (user z, assistant w) = 200 <= 250; adding assistant y -> 300 > 250 -> stop.
    assert _texts(out) == ["z" * 100, "w" * 100]
    assert _roles(out)[0] == "user"


def test_drops_leading_assistant_after_trim():
    # If the budget cut lands so the oldest kept is an assistant, it must be dropped so the
    # slice still starts with a user message.
    rows = _rows(("user", "u1"), ("assistant", "A" * 100),
                 ("user", "u2"), ("assistant", "a2"))
    # newest-first cumulative: a2(2), u2(4), A(104) all <= 105; u1 -> 106 > 105 so it's dropped.
    # kept newest-first [a2, u2, A] -> reversed [A(assistant), u2, a2] -> drop the leading assistant.
    out = _trim_history(rows, char_budget=105)
    assert _roles(out)[0] == "user"
    assert "A" * 100 not in _texts(out)


def test_oversized_latest_assistant_degrades_to_empty():
    # The newest stored message is always an assistant (the prior reply, since history loads before
    # the current user turn is inserted). If it alone exceeds the budget, history degrades to EMPTY
    # rather than starting with an assistant — the caller still appends the current user message, so
    # the turn continues, just without older context. (Never triggers at the 60K default.)
    rows = _rows(("user", "tiny"), ("assistant", "Z" * 5000))
    assert _trim_history(rows, char_budget=100) == []


def test_oversized_latest_user_is_kept():
    rows = _rows(("assistant", "old"), ("user", "Q" * 5000))
    out = _trim_history(rows, char_budget=100)
    assert _texts(out) == ["Q" * 5000] and _roles(out) == ["user"]


def test_empty():
    assert _trim_history([], char_budget=100) == []


def test_none_content_does_not_crash():
    rows = _rows(("user", None), ("assistant", "ok"))
    out = _trim_history(rows, char_budget=100)
    assert _roles(out)[0] == "user"
