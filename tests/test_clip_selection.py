"""Tests for the pure clip-selection helpers (windows, snapping, pricing)."""
from clip_selection import (
    build_transcript_windows,
    choose_distinct_clips,
    selection_limits,
    snap_clip_to_words,
    compact_words,
    lookup_model_prices,
)


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def _word(w, s, e):
    return {"w": w, "s": s, "e": e}


class TestBuildTranscriptWindows:
    def test_windows_align_to_segment_boundaries(self):
        transcript = {"segments": [
            _seg(0, 40, "a"), _seg(40, 80, "b"), _seg(80, 100, "c"), _seg(100, 150, "d"),
        ]}
        windows = build_transcript_windows(transcript, 150, window_seconds=90, overlap_seconds=30)
        segment_edges = {0, 40, 80, 100, 150}
        for w in windows:
            assert w["start"] in segment_edges
            assert w["end"] in segment_edges

    def test_windows_overlap(self):
        transcript = {"segments": [_seg(i * 10, (i + 1) * 10, f"s{i}") for i in range(30)]}
        windows = build_transcript_windows(transcript, 300, window_seconds=90, overlap_seconds=30)
        assert len(windows) >= 3
        for prev, nxt in zip(windows, windows[1:]):
            # next window starts before the previous one ends (overlap)
            assert nxt["start"] < prev["end"]
        # full coverage to the end
        assert windows[-1]["end"] == 300

    def test_empty_transcript_falls_back_to_full_video(self):
        windows = build_transcript_windows({"segments": []}, 120)
        assert len(windows) == 1
        assert windows[0]["start"] == 0.0
        assert windows[0]["end"] == 120

    def test_always_progresses(self):
        # One giant segment must not loop forever
        transcript = {"segments": [_seg(0, 500, "long monolog")]}
        windows = build_transcript_windows(transcript, 500, window_seconds=90, overlap_seconds=30)
        assert len(windows) == 1


class TestSelectionPolicy:
    def test_normal_video_keeps_current_shortlist(self):
        assert selection_limits(7199) == (10, 10)

    def test_two_hour_video_uses_wider_pool_but_caps_output(self):
        assert selection_limits(8809.16) == (15, 10)

    def test_overlapping_candidate_keeps_higher_score(self):
        clips = [
            {"start": 10.0, "end": 40.0, "predicted_score": 80, "name": "weak"},
            {"start": 12.0, "end": 39.0, "predicted_score": 92, "name": "strong"},
            {"start": 100.0, "end": 130.0, "predicted_score": 85, "name": "other"},
        ]
        selected = choose_distinct_clips(clips, max_clips=10)
        assert [clip["name"] for clip in selected] == ["strong", "other"]

    def test_output_is_never_padded_and_never_exceeds_ten(self):
        few = [
            {"start": index * 100.0, "end": index * 100.0 + 20.0, "predicted_score": 90}
            for index in range(8)
        ]
        many = [
            {"start": index * 100.0, "end": index * 100.0 + 20.0, "predicted_score": 90 - index}
            for index in range(15)
        ]
        assert len(choose_distinct_clips(few, max_clips=10)) == 8
        assert len(choose_distinct_clips(many, max_clips=10)) == 10


class TestSnapClipToWords:
    def _words(self):
        # words every ~2s with 0.4s gaps: [0,1.6], [2,3.6], [4,5.6], ...
        return [_word(f"w{i}", i * 2.0, i * 2.0 + 1.6) for i in range(40)]

    def test_start_snaps_into_silence_before_word(self):
        words = self._words()
        # Gemini proposes 10.3 — nearest word start is 10.0, gap before is 9.6->10.0
        start, end = snap_clip_to_words(10.3, 30.1, words, 80.0)
        assert 9.8 <= start <= 10.0  # word start minus half-gap lead
        # end 30.1 -> nearest word end 29.6 plus tail
        assert 29.6 <= end <= 30.05

    def test_no_words_nearby_keeps_original(self):
        words = [_word("far", 200.0, 201.0)]
        assert snap_clip_to_words(10.0, 40.0, words, 300.0) == (10.0, 40.0)

    def test_empty_words_keeps_original(self):
        assert snap_clip_to_words(5.0, 25.0, [], 100.0) == (5.0, 25.0)

    def test_duration_repaired_to_minimum(self):
        words = self._words()
        # snapping would yield ~14.4s; must be extended to >= 15s on a word end
        start, end = snap_clip_to_words(10.0, 24.5, words, 80.0)
        assert end - start >= 15.0

    def test_duration_capped_at_maximum(self):
        words = self._words()
        start, end = snap_clip_to_words(0.0, 59.9, words, 80.0)
        assert end - start <= 60.0


class TestPricing:
    def test_known_models(self):
        assert lookup_model_prices("gemini-2.5-flash") == (0.30, 2.50)
        assert lookup_model_prices("gemini-3-flash-preview") == (0.50, 3.00)

    def test_prefix_match_with_suffix(self):
        assert lookup_model_prices("gemini-2.5-flash-002") == (0.30, 2.50)

    def test_unknown_model_returns_none(self):
        assert lookup_model_prices("gpt-9-mega") is None
        assert lookup_model_prices(None) is None


class TestCompactWords:
    def test_rounds_timestamps(self):
        words = [{"w": " hi", "s": 17.240000000000002, "e": 17.899999999999999}]
        assert compact_words(words) == [{"w": " hi", "s": 17.24, "e": 17.9}]
