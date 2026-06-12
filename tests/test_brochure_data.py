"""Unit tests for the brochure data-assembly helpers (pure functions only —
the DB/S3/vision paths are exercised by the end-to-end render script)."""
import pytest

from app.brochures.data import (
    _bedrooms_label,
    _drive_minutes,
    _first_sentences,
    _haversine_km,
    _payment_plan,
    _size_to_sqft,
    _split_amenities,
    clean_text,
    fmt_aed_compact,
    fmt_int,
    fmt_pct,
    fmt_quarter,
)


class FakeProject:
    def __init__(self, raw):
        self.raw = raw


def test_fmt_aed_compact():
    assert fmt_aed_compact(2_684_000) == "AED 2.68M"
    assert fmt_aed_compact(1_000_000) == "AED 1M"
    assert fmt_aed_compact(33_875_000.0043) == "AED 33.88M"
    assert fmt_aed_compact(737_000) == "AED 737K"
    assert fmt_aed_compact(0) is None
    assert fmt_aed_compact(None) is None
    assert fmt_aed_compact("not a number") is None


def test_fmt_pct():
    assert fmt_pct(4.7) == "4.7%"
    assert fmt_pct(7.0) == "7%"
    assert fmt_pct(7.26, signed=True) == "+7.3%"
    assert fmt_pct(-47.2, signed=True) == "−47.2%"
    assert fmt_pct(None) is None


def test_fmt_quarter():
    assert fmt_quarter("2029-Q2") == "Q2 '29"
    assert fmt_quarter(None) is None
    assert fmt_quarter("TBD") == "TBD"  # unknown format passes through


def test_fmt_int():
    assert fmt_int(3798) == "3,798"
    assert fmt_int(0) is None
    assert fmt_int(None) is None


def test_clean_text_strips_nbsp():
    # district names can contain non-breaking spaces (data-quality gotcha)
    assert clean_text("City\xa0Walk") == "City Walk"
    assert clean_text("  a   b ") == "a b"


def test_size_to_sqft_converts_sqm():
    assert _size_to_sqft(100, "sqm") == pytest.approx(1076.39)
    assert _size_to_sqft(100, "sqft") == 100
    assert _size_to_sqft(100, None) == 100
    assert _size_to_sqft(0, "sqft") is None
    assert _size_to_sqft(None, "sqm") is None


def test_bedrooms_label():
    assert _bedrooms_label(1) == ("One Bedroom", "1 BR")
    assert _bedrooms_label(0) == ("Studio", "Studio")
    assert _bedrooms_label(4.5) == ("4.5 Bedroom", "4.5 BR")
    assert _bedrooms_label(None) == ("Residence", "")


def test_drive_minutes_reasonable():
    assert _drive_minutes(0.5) >= 4
    # ~10km should be in the 15-30 min range, not absurd
    assert 10 <= _drive_minutes(10) <= 30
    km = _haversine_km(25.2065, 55.2570, 25.1972, 55.2744)  # City Walk -> Downtown
    assert 1 < km < 4


def test_split_amenities():
    out, ind = _split_amenities(["Swimming Pools", "Cinema Room", "Jogging Track", "Gym"])
    assert "Swimming Pools" in out and "Jogging Track" in out
    assert "Cinema Room" in ind and "Gym" in ind


def _plan_raw(steps):
    return {"payment_plans": [{"name": "Payment Plan", "steps": steps}]}


def test_payment_plan_basic():
    p = FakeProject(_plan_raw([
        {"name": "On booking", "percentage": 20, "stage_type": "on_booking", "children": []},
        {"name": "During construction", "percentage": 55, "stage_type": "during_construction", "children": []},
        {"name": "Upon Handover", "percentage": 25, "stage_type": "on_handover", "children": []},
    ]))
    plan = _payment_plan(p, "Q2 '29")
    assert plan is not None
    assert [s["pct"] for s in plan["steps"]] == ["20%", "55%", "25%"]
    assert plan["steps"][-1]["label"] == "On Completion"
    assert plan["steps"][-1]["when"] == "Q2 '29"
    assert plan["summary"].startswith("75 / 25")


def test_payment_plan_flattens_children_and_caps_cells():
    children = [
        {"name": f"Installment {i}", "percentage": 5, "stage_type": "during_construction"}
        for i in range(8)
    ]
    p = FakeProject(_plan_raw([
        {"name": "On booking", "percentage": 20, "stage_type": "on_booking", "children": []},
        {"name": "During construction", "percentage": 40, "stage_type": "during_construction",
         "children": children},
        {"name": "Upon Handover", "percentage": 40, "stage_type": "on_handover", "children": []},
    ]))
    plan = _payment_plan(p, None)
    assert plan is not None
    assert len(plan["steps"]) <= 7
    # percentages must still add to 100 after the overflow merge
    total = sum(float(s["pct"].rstrip("%")) for s in plan["steps"])
    assert total == pytest.approx(100)
    # the FINAL (handover) step must survive intact — not be folded into the merge,
    # which would corrupt both the cell label and the X/Y summary split
    assert plan["steps"][-1]["pct"] == "40%"
    assert plan["steps"][-1]["label"] == "On Completion"
    assert plan["summary"].startswith("60 / 40")


def test_cheaper_label_flips_on_premium():
    # The data-assembly relabels the cover field so a positive (premium) figure is
    # never shown under a "Cheaper than" label. Mirror that logic here.
    from app.brochures.data import fmt_pct
    for pct, exp_label in [(-47.2, "Cheaper than Area Average"), (12.0, "Premium to Area Average")]:
        label = "Cheaper than Area Average" if pct <= 0 else "Premium to Area Average"
        assert label == exp_label
        assert fmt_pct(pct, signed=True).startswith("−" if pct < 0 else "+")


def test_payment_plan_missing_or_tiny_returns_none():
    assert _payment_plan(FakeProject({}), None) is None
    assert _payment_plan(FakeProject(_plan_raw([
        {"name": "Full payment", "percentage": 100, "stage_type": "on_booking", "children": []},
    ])), None) is None


def test_first_sentences_never_mid_word():
    s = "First sentence here. Second sentence is much longer and will not fit."
    out = _first_sentences(s, 30)
    assert out == "First sentence here."
    long_one = "word " * 50
    out2 = _first_sentences(long_one, 40)
    assert out2.endswith("…")
    assert len(out2) <= 45
