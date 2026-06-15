"""Unit tests for the handover label with construction fallback (app/brochures/data.py)."""
from datetime import date
from types import SimpleNamespace

from app.brochures.data import _date_quarter, handover_label


def _proj(**kw):
    base = dict(completion_quarter=None, completion_date=None,
                construction_end_date=None, readiness_progress=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_date_quarter():
    assert _date_quarter(date(2027, 1, 1)) == "Q1 '27"
    assert _date_quarter(date(2027, 9, 30)) == "Q3 '27"
    assert _date_quarter(date(2026, 12, 31)) == "Q4 '26"
    assert _date_quarter(None) is None


def test_prefers_explicit_quarter():
    assert handover_label(_proj(completion_quarter="2027-Q1",
                                construction_end_date=date(2099, 1, 1))) == "Q1 '27"


def test_falls_back_to_completion_date():
    assert handover_label(_proj(completion_date=date(2028, 4, 15))) == "Q2 '28"


def test_falls_back_to_construction_end_date():
    # no handover quarter or date -> the construction end date IS the anticipated handover
    assert handover_label(_proj(construction_end_date=date(2027, 12, 31))) == "Q4 '27"


def test_construction_date_beats_readiness():
    assert handover_label(_proj(construction_end_date=date(2028, 9, 30),
                                readiness_progress=3)) == "Q3 '28"


def test_readiness_progress_fallback():
    assert handover_label(_proj(readiness_progress=100)) == "Ready"
    assert handover_label(_proj(readiness_progress=62)) == "62% built"
    assert handover_label(_proj(readiness_progress=62.7)) == "63% built"


def test_zero_readiness_and_nothing_else_is_none():
    assert handover_label(_proj(readiness_progress=0)) is None
    assert handover_label(_proj()) is None


def test_bad_readiness_value_does_not_raise():
    assert handover_label(_proj(readiness_progress="n/a")) is None
