"""Unit tests for the outro append helpers (app/videos/outro.py). The ffmpeg concat itself needs
binaries + assets and is exercised at runtime; here we cover the pure orientation/transition logic
and that the branded outro assets ship with the repo."""
from app.videos import outro


def test_outro_asset_for_orientation():
    assert outro._outro_asset_for(1920, 1080) == outro.LANDSCAPE_OUTRO   # landscape
    assert outro._outro_asset_for(1080, 1920) == outro.PORTRAIT_OUTRO    # portrait
    assert outro._outro_asset_for(1000, 1000) == outro.LANDSCAPE_OUTRO   # square -> landscape


def test_outro_assets_present():
    assert outro.PORTRAIT_OUTRO.is_file(), "portrait outro asset missing from the repo"
    assert outro.LANDSCAPE_OUTRO.is_file(), "landscape outro asset missing from the repo"


def test_transition_duration_default_and_clamps():
    # Plenty long both ways -> the full target transition.
    assert outro._transition_duration(60.0, 6.0) == outro.TRANSITION_SEC
    # A short outro caps the transition to a third of it.
    assert outro._transition_duration(60.0, 0.9) == 0.3
    # A short main clip caps it too.
    assert outro._transition_duration(0.9, 6.0) == 0.3
    # Never drops below the 0.2s floor.
    assert outro._transition_duration(0.1, 0.1) == 0.2
