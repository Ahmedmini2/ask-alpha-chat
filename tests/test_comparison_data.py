"""Unit tests for the comparison data-assembly helpers (pure functions only —
the DB/S3/render paths are exercised by the end-to-end render script)."""
import pytest

from app.comparisons.data import (
    _alpha_score,
    _bedrooms_cell,
    _clamp,
    _price_per_sqft,
    _winner,
)


class FakeProject:
    def __init__(self, min_price=None, min_size=None, area_unit=None):
        self.min_price = min_price
        self.min_size = min_size
        self.area_unit = area_unit


def test_clamp():
    assert _clamp(5, 0, 10) == 5
    assert _clamp(-3, 0, 10) == 0
    assert _clamp(99, 0, 10) == 10


def test_bedrooms_cell():
    assert _bedrooms_cell([1, 2, 3, 4, 5]) == ("1–5", 5)
    assert _bedrooms_cell([0, 1, 2, 3]) == ("Studio–3", 3)
    assert _bedrooms_cell([3, 3, 3]) == ("3", 3)
    assert _bedrooms_cell([0]) == ("Studio", 0)
    assert _bedrooms_cell([2.5, 4]) == ("2.5–4", 4)
    assert _bedrooms_cell([]) == ("—", None)


def test_winner_min_and_max():
    # lower price/sqft is best
    assert _winner([3295, 1516, 8064], "min") == 1
    # largest area wins
    assert _winner([4575, 2180, 5107], "max") == 2


def test_winner_ties_and_edges():
    assert _winner([57, 57], "max") is None          # exact tie -> no badge
    assert _winner([7.0, 7.0, 6.1], "max") is None    # tie at the top
    assert _winner([5, None], "max") is None          # fewer than 2 present
    assert _winner([None, None], "min") is None
    assert _winner([1, 2], None) is None              # no direction (e.g. property type)
    assert _winner([1, 2, 3], "max") == 2


def test_winner_skips_missing_values():
    # missing middle value must not be picked or shift the index
    assert _winner([4.7, None, 6.1], "max") == 2
    assert _winner([4.7, None, 3.0], "max") == 0


def test_price_per_sqft_precedence():
    p = FakeProject(min_price=1_000_000, min_size=1000, area_unit="sqft")
    # explicit override wins
    assert _price_per_sqft({"asking_rate_aed_sqft": 1500}, p, {"price_per_sqft_aed": 1234}) == 1234
    # else the computed asking rate
    assert _price_per_sqft({"asking_rate_aed_sqft": 1500}, p, {}) == 1500
    # else derived from project min_price / min_size
    assert _price_per_sqft({}, p, {}) == pytest.approx(1000.0)
    # nothing to go on
    assert _price_per_sqft({}, FakeProject(), {}) is None


def test_price_per_sqft_converts_sqm_fallback():
    p = FakeProject(min_price=1_076_390, min_size=100, area_unit="sqm")
    # 100 sqm -> 1076.39 sqft, so rate ~= 1000 AED/sqft
    assert _price_per_sqft({}, p, {}) == pytest.approx(1000.0, rel=1e-3)


def test_alpha_score_override_wins_and_clamps():
    assert _alpha_score({}, {"alpha_score": 81}) == 81
    assert _alpha_score({}, {"alpha_score": 140}) == 100
    assert _alpha_score({}, {"alpha_score": -5}) == 0


def test_alpha_score_value_signal_direction():
    # a discount to the area median should score higher than a steep premium
    cheap = _alpha_score({"premium_to_market_pct": -20}, {})
    pricey = _alpha_score({"premium_to_market_pct": 200}, {})
    assert cheap is not None and pricey is not None
    assert cheap > pricey
    # bounded into the believable band
    assert 40 <= pricey <= 96 and 40 <= cheap <= 96


def test_alpha_score_none_without_any_signal():
    # no premium, no market, no yield band -> nothing to score
    assert _alpha_score({}, {}) is None
    # a yield band alone is enough to produce a score
    s = _alpha_score({"rental_yield_estimate": {"gross_yield_low_pct": 6, "gross_yield_high_pct": 8}}, {})
    assert s is not None and 40 <= s <= 96
