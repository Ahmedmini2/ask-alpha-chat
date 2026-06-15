"""Unit tests for the FAL whisper response mapping (pure — no network)."""
from app.integrations.fal import _words_from_result


def test_maps_word_chunks():
    payload = {"chunks": [
        {"timestamp": [0.0, 0.4], "text": " Wake"},
        {"timestamp": [0.4, 0.7], "text": "up"},
    ]}
    assert _words_from_result(payload) == [
        {"text": "Wake", "start": 0.0, "end": 0.4},
        {"text": "up", "start": 0.4, "end": 0.7},
    ]


def test_drops_missing_timestamps_and_text():
    payload = {"chunks": [
        {"timestamp": [None, None], "text": "x"},
        {"text": "no-ts"},
        {"timestamp": [1.0, 1.2], "text": "   "},
        {"timestamp": [1.0, 1.2], "text": "keep"},
    ]}
    assert _words_from_result(payload) == [{"text": "keep", "start": 1.0, "end": 1.2}]


def test_clamps_end_before_start():
    payload = {"chunks": [{"timestamp": [2.0, 1.0], "text": "oops"}]}
    out = _words_from_result(payload)
    assert out == [{"text": "oops", "start": 2.0, "end": 2.0}]


def test_empty_and_malformed_safe():
    assert _words_from_result({}) == []
    assert _words_from_result({"chunks": None}) == []
    assert _words_from_result({"chunks": ["not-a-dict"]}) == []
