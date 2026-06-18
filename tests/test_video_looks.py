"""Unit tests for the avatar-look helpers (pure functions only — the HeyGen network
paths are exercised by a dev-time inspection script)."""
import pytest

from app.integrations import heygen
from app.integrations.heygen import (
    _dedupe_look_names,
    _friendly_look_name,
    _match_group,
    _match_own_group,
    _normalize_look,
    find_look_in,
    looks_for_connected_avatar,
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
        "default_voice_id": None,
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


# ---------------------------- find_look_in (pure) ----------------------------

def _looks():
    return [
        {"look_name": "Original", "avatar_id": "a0"},
        {"look_name": "Dubai Executive", "avatar_id": "a1"},
        {"look_name": "The Golf Concierge", "avatar_id": "a2"},
    ]


def test_find_look_in_exact_then_substring_then_overlap():
    assert find_look_in(_looks(), "dubai executive")["avatar_id"] == "a1"   # exact (ci)
    assert find_look_in(_looks(), "golf")["avatar_id"] == "a2"              # substring
    assert find_look_in(_looks(), "executive dubai")["avatar_id"] == "a1"   # token overlap
    assert find_look_in(_looks(), "") is None
    assert find_look_in([], "anything") is None


# -------------------- looks_for_connected_avatar (locked to one group) --------------------

@pytest.mark.asyncio
async def test_connected_avatar_synthesizes_from_avatar_id_when_group_empty(monkeypatch):
    async def fake_group_looks(_gid):
        return []
    monkeypatch.setattr(heygen, "list_group_looks", fake_group_looks)
    looks = await looks_for_connected_avatar("g1", "av1", "https://x/p.png", "ahmed othman")
    assert len(looks) == 1
    assert looks[0]["avatar_id"] == "av1"
    assert looks[0]["look_name"] == "Original"   # name == person -> Original
    assert looks[0]["is_photo"] is True          # twins are talking-photo avatars
    assert looks[0]["preview_url"] == "https://x/p.png"


@pytest.mark.asyncio
async def test_connected_avatar_synthesizes_even_when_group_api_errors(monkeypatch):
    async def boom(_gid):
        raise heygen.HeyGenError("group looks failed 500")
    monkeypatch.setattr(heygen, "list_group_looks", boom)
    looks = await looks_for_connected_avatar("g1", "av1", None, "ahmed othman")
    assert [l["avatar_id"] for l in looks] == ["av1"]


@pytest.mark.asyncio
async def test_connected_avatar_lists_group_looks_and_dedupes_primary(monkeypatch):
    # The group returns two looks, one of which IS the known avatar_id — it must appear once.
    async def fake_group_looks(_gid):
        return [
            {"avatar_id": "av1", "avatar_name": "Original", "preview_image_url": "https://x/0.png"},
            {"id": "av2", "name": "Dubai Executive", "image_url": "https://x/1.webp", "status": "completed"},
        ]
    monkeypatch.setattr(heygen, "list_group_looks", fake_group_looks)
    looks = await looks_for_connected_avatar("g1", "av1", None, "ahmed othman")
    ids = [l["avatar_id"] for l in looks]
    assert ids.count("av1") == 1          # primary not duplicated
    assert set(ids) == {"av1", "av2"}
    assert any(l["look_name"] == "Dubai Executive" for l in looks)


# ---------------- _match_own_group (SAFE self-identity resolver — security) ----------------

def _groups():
    return [
        {"id": "g1", "name": "Zain Ul Abdeen", "num_looks": 21},
        {"id": "g2", "name": "Ahmed Khan", "num_looks": 5},
        {"id": "g3", "name": "Ramy Nabil", "num_looks": 1},
    ]


def test_match_own_group_exact_full_name():
    assert _match_own_group(_groups(), {"Zain Ul Abdeen"})["id"] == "g1"
    assert _match_own_group(_groups(), {"ramy nabil"})["id"] == "g3"   # case-insensitive


def test_match_own_group_rejects_same_first_name_collision():
    # THE bug the review caught: 'Ahmed Othman' (no group of his own) must NOT resolve to 'Ahmed
    # Khan' just because they share the first name. Old _match_group would have matched g2.
    assert _match_own_group(_groups(), {"Ahmed Othman", "ahmed.othman"}) is None
    # and _match_group (the unsafe free-text matcher) indeed WOULD have matched it — proving the
    # new resolver is strictly safer:
    assert _match_group(_groups(), "Ahmed Othman")["id"] == "g2"


def test_match_own_group_never_single_token_matches_a_fuller_group():
    # a bare first name must not subset-match a full-name group
    assert _match_own_group(_groups(), {"Ahmed"}) is None
    assert _match_own_group(_groups(), {"Zain"}) is None


def test_match_own_group_allows_unique_multitoken_subset():
    groups = [{"id": "g1", "name": "Zain Ul Abdeen Official", "num_looks": 3}]
    # full identity name is a token-subset of the (longer) group name, and it's unique → safe match
    assert _match_own_group(groups, {"Zain Ul Abdeen"})["id"] == "g1"


def test_match_own_group_refuses_ambiguous_subset():
    groups = [
        {"id": "g1", "name": "Zain Ul Abdeen", "num_looks": 3},
        {"id": "g2", "name": "Zain Ul Abdeen Junior", "num_looks": 9},
    ]
    # two distinct groups are token-compatible with 'Zain Ul' → refuse rather than guess
    assert _match_own_group(groups, {"Zain Ul"}) is None


def test_match_own_group_empty_identity():
    assert _match_own_group(_groups(), set()) is None
    assert _match_own_group(_groups(), {""}) is None


@pytest.mark.asyncio
async def test_connected_avatar_never_calls_group_when_no_group_id(monkeypatch):
    called = {"n": 0}
    async def spy(_gid):
        called["n"] += 1
        return []
    monkeypatch.setattr(heygen, "list_group_looks", spy)
    looks = await looks_for_connected_avatar(None, "av1", None, "ahmed")
    assert called["n"] == 0                # no group_id -> no account-wide lookup at all
    assert [l["avatar_id"] for l in looks] == ["av1"]
