"""Unit tests for the area-model investment metrics (pure functions only —
the DB-backed real-data gathering is covered by integration runs)."""
import pytest

from app.analytics.property_metrics import (
    compute_metrics,
    resolve_community,
)


def test_resolve_community_known_and_aliases():
    assert resolve_community("Dubai Marina") == ("dubai-marina", True)
    assert resolve_community("Downtown Dubai") == ("downtown", True)
    assert resolve_community("Jumeirah Village Circle") == ("jvc", True)
    assert resolve_community("JVC") == ("jvc", True)
    assert resolve_community("Dubai Hills Estate") == ("dubai-hills", True)
    # non-breaking space in the name must not defeat the match (data-quality gotcha)
    assert resolve_community("Business\xa0Bay") == ("business-bay", True)


def test_resolve_community_fallback():
    slug, matched = resolve_community("Sharjah Waterfront")
    assert slug == "dubai-marina"  # Dubai Marina fallback, by design
    assert matched is False
    assert resolve_community("") == ("dubai-marina", False)
    assert resolve_community(None) == ("dubai-marina", False)


def test_worked_example_matches_website():
    # 2BR, Dubai Marina, AED 2,700,000, 1,200 sqft — the doc's worked example.
    m = compute_metrics(2_700_000, beds=2, sqft=1200, community="Dubai Marina")
    assert m["net_yield_pct"] == 5.4
    assert m["area_avg_rent_return_pct"] == 6.2
    assert m["annual_appreciation_pct"] == 7.2
    assert m["y5_projected_value_aed"] == pytest.approx(3_822_000, abs=1000)  # 2.7M·1.072^5
    assert m["five_year_gain_pct"] == pytest.approx(41.6, abs=0.1)
    assert m["time_to_sell_days"] == 90
    assert m["vs_area_price_pct"] == pytest.approx(-2.2, abs=0.1)
    assert m["used_fallback"] is False


def test_bed_factor_edges():
    # studio/1BR get the 1.05 uplift, 4BR+ get the 0.94 haircut
    one = compute_metrics(2_700_000, beds=1, sqft=1200, community="Dubai Marina")
    four = compute_metrics(2_700_000, beds=4, sqft=1200, community="Dubai Marina")
    mid = compute_metrics(2_700_000, beds=2, sqft=1200, community="Dubai Marina")
    assert one["net_yield_pct"] > mid["net_yield_pct"] > four["net_yield_pct"]


def test_offplan_path_uses_area_yield_directly():
    # No beds/sqft -> net yield is the area yield (the website's off-plan behaviour)
    m = compute_metrics(2_700_000, community="Dubai Marina")
    assert m["net_yield_pct"] == 6.2
    assert m["net_yield_pct"] == m["area_avg_rent_return_pct"]


def test_real_data_overrides_win_and_are_tagged():
    m = compute_metrics(
        2_700_000, beds=2, sqft=1200, community="Dubai Marina",
        area_yield=0.07, area_appreciation=0.10, area_ppsf=2000, activity_label="hot",
    )
    assert m["area_avg_rent_return_pct"] == 7.0           # real band, not table 6.2
    assert m["annual_appreciation_pct"] == 10.0           # real appreciation
    assert m["time_to_sell_days"] == 45                   # 'hot' activity, not 90
    assert m["vs_area_price_pct"] == pytest.approx(12.5)  # 2250 vs real 2000 ppsf
    assert m["sources"]["area_avg_rent_return"] == "real"
    assert m["sources"]["annual_appreciation"] == "real"
    assert m["sources"]["time_to_sell"] == "real"
    assert m["sources"]["vs_area_price"] == "real"


def test_fallback_tags_sources():
    m = compute_metrics(1_000_000, beds=2, sqft=800, community="Some Unknown Place")
    assert m["used_fallback"] is True
    assert m["sources"]["annual_appreciation"] == "area_model_fallback"
    assert m["community_matched"] == "Dubai Marina"


def test_no_price_returns_none():
    assert compute_metrics(0, community="Dubai Marina") is None
    assert compute_metrics(None) is None
    assert compute_metrics("nope") is None
