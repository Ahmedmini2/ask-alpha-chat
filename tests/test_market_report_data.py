"""Unit tests for the Dubai Market Report data helpers (pure functions only — the DB gather
and Chromium render are exercised by a dev-time script)."""
from app.reports.market_data import (
    _slug_label,
    fmt_money,
    fmt_pct,
    sparkline_points,
)


def test_fmt_money():
    assert fmt_money(1658) == "AED 1,658"
    assert fmt_money(1_150_000) == "AED 1.15M"
    assert fmt_money(1_856_000) == "AED 1.86M"
    assert fmt_money(0) == "AED 0"
    assert fmt_money(None) == "—"
    assert fmt_money("not a number") == "—"
    assert fmt_money(3564, aed=False) == "3,564"


def test_fmt_pct():
    assert fmt_pct(4.78, signed=True, decimals=2) == "+4.78%"
    assert fmt_pct(-0.74, signed=True, decimals=2) == "-0.74%"
    assert fmt_pct(5.3) == "5.3%"
    assert fmt_pct(None) == "—"
    assert fmt_pct(0, signed=True) == "+0.0%"


def test_sparkline_points_maps_inside_box_and_inverts_y():
    pts = sparkline_points([100, 200], 100, 20, pad=0).split(" ")
    # 2 points: first at x=0, last at x=100; lowest value -> bottom (y=20), highest -> top (y=0)
    assert pts[0] == "0.0,20.0"
    assert pts[1] == "100.0,0.0"


def test_sparkline_monotonic_x_and_within_bounds():
    vals = [100, 150, 120, 231, 90, 260]
    pts = [tuple(map(float, p.split(","))) for p in sparkline_points(vals, 200, 50).split(" ")]
    xs = [x for x, _ in pts]
    assert xs == sorted(xs)                      # x strictly increasing
    assert all(0 <= x <= 200 for x, _ in pts)
    assert all(0 <= y <= 50 for _, y in pts)
    assert len(pts) == len(vals)


def test_sparkline_empty_and_degenerate():
    assert sparkline_points([], 100, 20) == ""
    assert sparkline_points([None, "x"], 100, 20) == ""
    # a flat series shouldn't divide by zero — all points land on one horizontal line
    flat = sparkline_points([50, 50, 50], 100, 20)
    assert flat and "nan" not in flat.lower()


def test_slug_label_prettifies_slugs_but_keeps_curated():
    assert _slug_label("dubai-land-residence-complex", "x") == "Dubai Land Residence Complex"
    assert _slug_label("", "dubai-marina") == "Dubai Marina"
    assert _slug_label("JVC (Jumeirah Village Circle)", "jvc") == "JVC (Jumeirah Village Circle)"
    assert _slug_label("Dubai Hills Estate", "dubai-hills") == "Dubai Hills Estate"
    assert _slug_label(None, None) == "—"
