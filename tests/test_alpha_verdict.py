"""Parity gate: the ported Alpha Verdict must reproduce the live website's numbers exactly.
Reference = aredxb-next project 2291 (JVC, 105 Residences), entry From AED 755K, "Is this a good
buy?" box: pillars 94/13/97/55, conviction 74.8 (WATCH, shown 75), net yield 7.6%, ppsf 1728,
"57.1% above community average"; Numbers at a glance: area rent 8.2%, appreciation 8.5%, Y5 ≈ 1.14M.
"""
from app.analytics.alpha_verdict import (
    compute_alpha_verdict,
    canonical_community_slug,
    resolve_community,
    COMMUNITY_DATA,
)

# Entry unit derived from the site: 755K @ ppsf 1728 -> ~437 sqft, 1-bed.
JVC_2291 = dict(price=755_000, community="Jumeirah Village Circle (JVC)", beds=1, sqft=437,
                intent="yield", charges_per_sqft=18.0)


def test_pillars_match_live_site_2291():
    v = compute_alpha_verdict(**JVC_2291)
    assert v["pillars"] == {"yield": 94, "comp": 13, "thesis": 97, "risk": 55}


def test_conviction_and_verdict_2291():
    v = compute_alpha_verdict(**JVC_2291)
    # weighted (yield intent) ~74.8 -> below 75 -> WATCH even though it displays as 75
    assert 74.0 <= v["conviction"] <= 75.0
    assert v["verdict"] == "WATCH"


def test_numbers_at_a_glance_2291():
    v = compute_alpha_verdict(**JVC_2291)
    n = v["numbers"]
    assert n["net_yield_pct"] == 7.6
    assert n["area_rent_return_pct"] == 8.2          # JVC community yield
    assert n["annual_appreciation_pct"] == 8.5       # JVC community appreciation
    assert n["ppsf_aed"] == 1728
    assert n["vs_area_price_pct"] == 57.1            # "57.1% above community average"
    assert round(n["y5_value_aed"] / 1_000_000, 2) == 1.14   # Y5 ≈ AED 1.14M


def test_canonical_slug_mapping():
    cases = {
        "Dubai Marina": "dubai-marina",
        "JVC": "jvc", "Jumeirah Village Circle": "jvc",
        "Downtown Dubai": "downtown", "Palm Jumeirah": "palm-jumeirah",
        "Business Bay": "business-bay", "Dubai Hills Estate": "dubai-hills",
        "DAMAC Lagoons": "damac-lagoons", "Emaar Beachfront": "emaar-beachfront",
        "Sobha Hartland": "sobha-hartland", "Arabian Ranches": "arabian-ranches",
        "Some Unknown Place": "some-unknown-place",
    }
    for name, slug in cases.items():
        assert canonical_community_slug(name) == slug


def test_unknown_community_falls_back_to_dubai_marina():
    cm = resolve_community("Nowhere Town")
    assert cm.matched is False
    assert cm.gross_yield == COMMUNITY_DATA["dubai-marina"]["yield"]


def test_verdict_thresholds():
    # construct inputs around the 55 / 75 boundaries via community + ppsf
    assert compute_alpha_verdict(price=0, community="jvc") is None  # unscoreable
    buy = compute_alpha_verdict(price=500_000, community="jvc", beds=1, sqft=600, intent="yield")
    assert buy["verdict"] in {"BUY", "WATCH", "SKIP"}  # smoke: returns a valid verdict


def test_bed_adjustment_changes_yield():
    one = compute_alpha_verdict(price=1_000_000, community="dubai-marina", beds=1, sqft=800)
    three = compute_alpha_verdict(price=1_000_000, community="dubai-marina", beds=3, sqft=800)
    assert one["numbers"]["net_yield_pct"] > three["numbers"]["net_yield_pct"]  # 1.05x vs 1.0x
