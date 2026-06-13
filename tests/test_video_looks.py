"""Unit tests for the avatar-look helpers (pure functions only — the HeyGen network
paths are exercised by a dev-time inspection script)."""
from app.integrations.heygen import (
    _dedupe_look_names,
    _friendly_look_name,
    _match_group,
    _normalize_look,
)


def test_friendly_look_name():
    # curated photo-avatar names pass straight through
    assert _friendly_look_name("Dubai Executive", "Zain") == "Dubai Executive"
    assert _friendly_look_name("The Golf Concierge", "Zain") == "The Golf Concierge"
    # empty / person-name-only becomes "Original"
    assert _friendly_look_name("", "Zain") == "Original"
    assert _friendly_look_name(None, "Zain") == "Original"
    assert _friendly_look_name("Zain", "Zain") == "Original"
    assert _friendly_look_name("  zain ", "Zain") == "Original"  # normalized equality
    # internal whitespace collapses
    assert _friendly_look_name("Dapper   Urban  Executive", "Zain") == "Dapper Urban Executive"


def test_match_group_exact_then_first_token():
    groups = [
        {"id": "g1", "name": "Old Guy", "num_looks": 4},
        {"id": "g2", "name": "Zain Ul Abdeen", "num_looks": 21},
        {"id": "g3", "name": "Ramy Nabil", "num_looks": 1},
    ]
    # exact full-name match
    assert _match_group(groups, "Ramy Nabil")["id"] == "g3"
    # first-name token match ("Zain" -> "Zain Ul Abdeen")
    assert _match_group(groups, "Zain")["id"] == "g2"
    # profile carrying the full name still matches
    assert _match_group(groups, "Zain Ul Abdeen")["id"] == "g2"
    # nothing remotely matching
    assert _match_group(groups, "Hermione Granger") is None
    assert _match_group(groups, "") is None


def test_match_group_exact_beats_first_token():
    groups = [
        {"id": "a", "name": "Zain", "num_looks": 1},
        {"id": "b", "name": "Zain Ul Abdeen", "num_looks": 21},
    ]
    # an exact-name group wins even if a first-token match has more looks
    assert _match_group(groups, "Zain")["id"] == "a"


def test_match_group_prefers_more_looks_within_a_tier():
    groups = [
        {"id": "a", "name": "Zain Junior", "num_looks": 1},
        {"id": "b", "name": "Zain Ul Abdeen", "num_looks": 21},
    ]
    # neither is exact; both share first token "zain" -> prefer the richer group
    assert _match_group(groups, "Zain")["id"] == "b"


def test_normalize_look_photo_shape():
    raw = {"id": "abc", "name": "Dubai Executive", "image_url": "https://x/y.webp", "status": "completed"}
    out = _normalize_look(raw, "Zain")
    assert out == {
        "avatar_id": "abc",
        "look_name": "Dubai Executive",
        "preview_url": "https://x/y.webp",
        "is_photo": True,
    }


def test_normalize_look_standard_avatar_shape():
    raw = {"avatar_id": "std1", "avatar_name": "Ramy Nabil", "preview_image_url": "https://x/r.png"}
    out = _normalize_look(raw, "Ramy Nabil")
    assert out["avatar_id"] == "std1"
    assert out["is_photo"] is False
    assert out["preview_url"] == "https://x/r.png"
    assert out["look_name"] == "Original"  # look name == person -> Original


def test_normalize_look_skips_unfinished_and_idless():
    assert _normalize_look({"id": "x", "status": "pending"}, "Zain") is None
    assert _normalize_look({"id": "x", "status": "training"}, "Zain") is None
    assert _normalize_look({"name": "no id here", "status": "completed"}, "Zain") is None
    # status absent (None) is allowed through (standard avatars don't report one)
    assert _normalize_look({"avatar_id": "ok"}, "Zain") is not None


def test_dedupe_look_names():
    looks = [
        {"look_name": "Zain in his studio space"},
        {"look_name": "Dubai Executive"},
        {"look_name": "Zain in his studio space"},
        {"look_name": "Zain in his studio space"},
    ]
    out = [lk["look_name"] for lk in _dedupe_look_names(looks)]
    assert out == [
        "Zain in his studio space",
        "Dubai Executive",
        "Zain in his studio space (2)",
        "Zain in his studio space (3)",
    ]
