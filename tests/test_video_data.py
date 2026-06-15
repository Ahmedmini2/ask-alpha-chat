"""Unit tests for the promo-video pure helpers (no network/DB — the HeyGen, Bedrock
image, and DB paths are exercised live). Mirrors tests/test_brochure_data.py: only the
pure functions are covered here."""
import pytest

from app.tools.videos import (
    _campaign_brief,
    _compose_background_prompt,
    _to_spoken_money,
)


# ----------------------------- _to_spoken_money -----------------------------

def test_aed_before_amount_becomes_dirhams_after():
    assert _to_spoken_money("Starting price AED 1.35 million today.") == \
        "Starting price 1.35 million dirhams today."


def test_aed_after_amount_becomes_dirhams():
    assert _to_spoken_money("From 850 thousand AED only.") == "From 850 thousand dirhams only."


def test_magnitude_abbreviations_are_spelled_out():
    assert _to_spoken_money("Just AED 1.4M now.") == "Just 1.4 million dirhams now."
    assert _to_spoken_money("From AED 850K.") == "From 850 thousand dirhams."


def test_plain_numeric_amount_keeps_commas():
    assert _to_spoken_money("Priced from AED 1,350,000 today.") == \
        "Priced from 1,350,000 dirhams today."


def test_no_currency_is_unchanged():
    assert _to_spoken_money("Submit your EOI today.") == "Submit your EOI today."


def test_never_leaves_the_aed_code():
    out = _to_spoken_money("From AED1.1M to AED 2.3 million in AED.")
    assert "AED" not in out
    assert "dirhams" in out


def test_empty_script_safe():
    assert _to_spoken_money("") == ""


class FakeUnit:
    def __init__(self, bedrooms):
        self.bedrooms = bedrooms


class FakeProject:
    """Minimal stand-in — every attribute _campaign_brief / _compose_background_prompt
    reads, defaulting to None/empty so individual tests override only what they need."""
    _FIELDS = (
        "name developer district city region country short_description description "
        "amenities units_count furnishing service_charge min_price currency "
        "completion_quarter sale_status has_escrow deposit_description post_handover"
    ).split()

    def __init__(self, **kwargs):
        for f in self._FIELDS:
            setattr(self, f, kwargs.get(f))
        self.units = kwargs.get("units", [])


# ----------------------------- _compose_background_prompt -----------------------------

def test_background_prompt_leads_with_user_text_and_appends_style():
    p = FakeProject(name="Damac District", district="Damac Hills", city="Dubai")
    out = _compose_background_prompt(p, "Malibu Bay lagoon with golf greens behind it")
    assert out.startswith("Malibu Bay lagoon with golf greens behind it")
    # cinematic style + people-exclusion suffix is always appended
    assert "photorealistic" in out
    assert "no people" in out
    assert "9:16" in out


def test_background_prompt_fallback_uses_location_and_name():
    p = FakeProject(name="Damac District", district="Damac Hills", city="Dubai")
    out = _compose_background_prompt(p, "")
    assert "in Damac Hills Dubai" in out
    assert "Damac District" in out
    assert "no people" in out


def test_background_prompt_fallback_defaults_to_dubai_when_no_location():
    p = FakeProject(name="Some Tower")
    out = _compose_background_prompt(p, None)
    assert "in Dubai" in out


def test_background_prompt_normalises_nbsp_in_district():
    # district names can carry non-breaking spaces (known data gotcha)
    p = FakeProject(name="X", district="Damac\xa0Hills", city=None)
    out = _compose_background_prompt(p, "")
    assert "Damac\xa0Hills" not in out
    assert "in Damac Hills" in out


def test_background_prompt_is_length_capped():
    p = FakeProject(name="X")
    out = _compose_background_prompt(p, "skyline " * 500)
    assert len(out) <= 1400


# ----------------------------------- _campaign_brief ----------------------------------

def test_campaign_brief_bedroom_range():
    p = FakeProject(name="X", units=[FakeUnit(0), FakeUnit(1), FakeUnit(2)])
    assert "- Bedrooms: studio–2" in _campaign_brief(p)


def test_campaign_brief_bedroom_single_value():
    p = FakeProject(name="X", units=[FakeUnit(1), FakeUnit(1)])
    brief = _campaign_brief(p)
    assert "- Bedrooms: 1" in brief
    # single value, not a range
    assert "1–1" not in brief


def test_campaign_brief_includes_furnishing_and_service_charge():
    p = FakeProject(name="X", furnishing="Semi-furnished", service_charge="AED 16–18 / sqft")
    brief = _campaign_brief(p)
    assert "- Furnishing: Semi-furnished" in brief
    assert "- Service charge: AED 16–18 / sqft" in brief


def test_campaign_brief_omits_missing_facts():
    brief = _campaign_brief(FakeProject(name="X"))
    assert "Bedrooms" not in brief
    assert "Furnishing" not in brief
    assert brief == "- Project: X"
