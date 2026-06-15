"""Unit tests for the b-roll planners (pure, no ffmpeg / network / DB). These pin the timeline
math and the filtergraph shape — the parts most worth locking down; the ffmpeg encode itself is
verified on a live run (see the b-roll plan)."""
from app.videos.broll import (
    BrollConfig,
    Segment,
    plan_segments,
    plan_image_sources,
    _build_filter_complex,
)

CFG = BrollConfig()  # defaults


def _check_invariants(segs, dur):
    assert segs, "never empty"
    assert abs(segs[0].start) < 1e-6, "starts at 0"
    assert abs(segs[-1].end - dur) < 1e-6, "ends at dur"
    assert segs[0].kind == "avatar" and segs[-1].kind == "avatar", "avatar holds head+tail"
    for a, b in zip(segs, segs[1:]):
        assert abs(a.end - b.start) < 1e-6, "contiguous, gap-free"
    for s in segs:
        assert s.end > s.start, "positive duration"


def test_short_video_has_no_broll():
    segs = plan_segments(8.0, 5, CFG)  # below min_total_dur (12)
    assert [s.kind for s in segs] == ["avatar"]
    assert not any(s.kind == "broll" for s in segs)


def test_zero_images_has_no_broll():
    segs = plan_segments(40.0, 0, CFG)
    assert not any(s.kind == "broll" for s in segs)


def test_normal_video_tiles_the_middle():
    dur = 40.0
    segs = plan_segments(dur, 5, CFG)
    _check_invariants(segs, dur)
    broll = [s for s in segs if s.kind == "broll"]
    assert len(broll) == 5
    # image indices are 0..k-1 in order
    assert [s.image_index for s in broll] == [0, 1, 2, 3, 4]
    # each b-roll clip within [min,max] segment bounds
    for s in broll:
        assert CFG.min_segment - 1e-6 <= (s.end - s.start) <= CFG.max_segment + 1e-6


def test_fewer_images_inserts_avatar_breathers():
    dur = 40.0
    segs = plan_segments(dur, 2, CFG)
    _check_invariants(segs, dur)
    broll = [s for s in segs if s.kind == "broll"]
    assert len(broll) == 2
    for s in broll:                      # capped at max_segment, not one long still
        assert (s.end - s.start) <= CFG.max_segment + 1e-6


def test_very_long_video_caps_segments():
    dur = 180.0
    segs = plan_segments(dur, 2, CFG)
    _check_invariants(segs, dur)
    for s in segs:
        if s.kind == "broll":
            assert (s.end - s.start) <= CFG.max_segment + 1e-6


def test_head_and_tail_respect_bounds():
    dur = 200.0
    segs = plan_segments(dur, 5, CFG)
    head = segs[0].end - segs[0].start
    # head*ratio (50s) exceeds head_max, so it clamps to head_max.
    assert head <= CFG.head_max + 1e-6
    # The tail is at least tail_min; it may be LARGER because the final avatar segment absorbs
    # any trailing breather time (adjacent avatars merge) — that's intended for long, low-image
    # videos (mostly avatar with brief property cutaways).
    tail = segs[-1].end - segs[-1].start
    assert segs[-1].kind == "avatar"
    assert tail >= CFG.tail_min - 1e-6


def test_plan_segments_is_deterministic():
    assert plan_segments(37.0, 4, CFG) == plan_segments(37.0, 4, CFG)


def test_plan_image_sources():
    assert plan_image_sources(10, 5, CFG) == (5, 0)     # plenty of photos
    assert plan_image_sources(2, 5, CFG) == (2, 3)      # top up with AI
    assert plan_image_sources(0, 5, CFG) == (0, 5)      # all AI
    assert plan_image_sources(99, 99, CFG)[0] <= CFG.max_clips  # capped


def test_build_filter_complex_shape():
    segs = plan_segments(40.0, 5, CFG)
    fc, broll_order = _build_filter_complex(segs, 1080, 1920, 30.0, CFG, seed=7)
    assert fc.count("concat=n=") == 1          # exactly one concat
    assert fc.count("zoompan") == 5            # one per b-roll still
    assert fc.count("trim=") == 2              # head + tail avatar trims
    assert broll_order == [0, 1, 2, 3, 4]      # still-input order matches image indices
    assert fc.strip().endswith("[vout]")


def test_build_filter_complex_input_indices_in_order():
    # b-roll stills are inputs 1..k, in segment order
    segs = plan_segments(40.0, 3, CFG)
    fc, order = _build_filter_complex(segs, 1080, 1920, 30.0, CFG, seed=0)
    assert "[1:v]" in fc and "[2:v]" in fc
    assert len(order) == sum(1 for s in segs if s.kind == "broll")
