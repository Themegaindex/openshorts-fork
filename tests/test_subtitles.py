"""Tests for subtitle word merging, SRT generation and style sanitizing."""
from subtitles import (
    _ass_time,
    _collect_word_blocks,
    merge_continuation_words,
    generate_srt,
    hex_to_ass_color,
    _sanitize_font_name,
    _clamp_number,
)


def _w(text, start, end):
    return {"word": text, "start": start, "end": end}


class TestMergeContinuationWords:
    def test_merges_compound_fragments(self):
        # faster-whisper splits "YouTube-Kanal." into two tokens; the second
        # one has no leading space and belongs to the first.
        words = [_w(" YouTube", 0.0, 0.5), _w("-Kanal.", 0.5, 0.9), _w(" ist", 1.0, 1.2)]
        merged = merge_continuation_words(words)
        assert [m["word"] for m in merged] == [" YouTube-Kanal.", " ist"]
        assert merged[0]["start"] == 0.0
        assert merged[0]["end"] == 0.9

    def test_keeps_real_word_boundaries(self):
        # Words with a leading space are separate words and must never be glued.
        words = [_w(" ich", 0.0, 0.2), _w(" habe", 0.2, 0.4)]
        merged = merge_continuation_words(words)
        assert [m["word"] for m in merged] == [" ich", " habe"]

    def test_first_word_without_space_stays(self):
        words = [_w("Hallo", 0.0, 0.2), _w(" Welt", 0.2, 0.4)]
        merged = merge_continuation_words(words)
        assert [m["word"] for m in merged] == ["Hallo", " Welt"]

    def test_number_fragments(self):
        words = [_w(" 1", 0.0, 0.2), _w(".200", 0.2, 0.4)]
        merged = merge_continuation_words(words)
        assert [m["word"] for m in merged] == [" 1.200"]

    def test_input_not_mutated(self):
        words = [_w(" a", 0.0, 0.1), _w("-b", 0.1, 0.2)]
        merge_continuation_words(words)
        assert words[0]["word"] == " a"
        assert words[1]["word"] == "-b"


class TestGenerateSrt:
    def _transcript(self, words):
        return {"segments": [{"start": 0, "end": 99, "text": "", "words": words}]}

    def test_no_orphan_fragments_in_srt(self, tmp_path):
        out = tmp_path / "subs.srt"
        words = [
            _w(" Mein", 0.0, 0.3),
            _w(" YouTube", 0.3, 0.8),
            _w("-Kanal.", 0.8, 1.1),
            _w(" ich", 1.2, 1.4),
            _w(" habe", 1.4, 1.7),
        ]
        assert generate_srt(self._transcript(words), 0, 10, str(out)) is True
        srt = out.read_text(encoding="utf-8-sig")
        assert "YouTube-Kanal." in srt
        assert " -Kanal" not in srt
        assert "ich habe" in srt
        assert "ichhabe" not in srt

    def test_empty_range_returns_false(self, tmp_path):
        out = tmp_path / "subs.srt"
        words = [_w(" spaet", 50.0, 50.5)]
        assert generate_srt(self._transcript(words), 0, 10, str(out)) is False

    def test_timeline_is_sorted_clipped_and_deduplicated(self):
        transcript = self._transcript([
            _w(" spaet", 1.0, 1.2),
            _w(" frueh", -0.2, 0.3),
            _w(" frueh", -0.195, 0.305),  # same token within 20 ms
            _w(" mitte", 0.5, 0.7),
        ])
        blocks = _collect_word_blocks(transcript, 0, 2, max_chars=100)
        words = [word for block in blocks for word in block]
        assert [word["word"] for word in words] == ["frueh", "mitte", "spaet"]
        assert words[0]["start"] == 0.0
        assert all(a["start"] <= b["start"] for a, b in zip(words, words[1:]))


class TestAssTiming:
    def test_centisecond_rounding_carries_into_next_second(self):
        assert _ass_time(0.995) == "0:00:01.00"
        assert _ass_time(59.995) == "0:01:00.00"


class TestStyleSanitizing:
    def test_invalid_hex_falls_back_to_white(self):
        assert hex_to_ass_color("#GGGGGG") == hex_to_ass_color("#FFFFFF")
        assert hex_to_ass_color("abc") == hex_to_ass_color("#FFFFFF")
        assert hex_to_ass_color(None) == hex_to_ass_color("#FFFFFF")

    def test_invalid_hex_custom_fallback(self):
        assert hex_to_ass_color("nope", fallback="000000") == hex_to_ass_color("#000000")

    def test_valid_hex_converts(self):
        # #RRGGBB -> &HAABBGGRR
        assert hex_to_ass_color("#FF0000", 1.0) == "&H000000FF"
        assert hex_to_ass_color("00FF00", 1.0) == "&H0000FF00"

    def test_opacity_clamped(self):
        assert hex_to_ass_color("#FFFFFF", 5.0) == hex_to_ass_color("#FFFFFF", 1.0)
        assert hex_to_ass_color("#FFFFFF", -1) == hex_to_ass_color("#FFFFFF", 0.0)

    def test_font_name_injection_stripped(self):
        assert _sanitize_font_name("Arial,Fontsize=99{\\b1}") == "ArialFontsize99b1"
        assert _sanitize_font_name("Comic Sans MS") == "Comic Sans MS"

    def test_font_name_empty_falls_back(self):
        assert _sanitize_font_name("") == "Verdana"
        assert _sanitize_font_name(",,{}") == "Verdana"
        assert _sanitize_font_name(None) == "Verdana"

    def test_clamp_number(self):
        assert _clamp_number(5, 0, 10, 1) == 5
        assert _clamp_number(99, 0, 10, 1) == 10
        assert _clamp_number(-3, 0, 10, 1) == 0
        assert _clamp_number("kaputt", 0, 10, 1) == 1
        assert _clamp_number(None, 0, 10, 1) == 1


class TestGenerateAss:
    from subtitles import generate_ass  # noqa: F401 (import check)

    def _transcript(self, words):
        return {"segments": [{"start": 0, "end": 99, "text": "", "words": words}]}

    def test_karaoke_events_highlight_each_word(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" Erst", 0.0, 0.3), _w(" mal", 0.3, 0.6), _w(" hier", 0.6, 0.9)]
        assert generate_ass(self._transcript(words), 0, 10, str(out),
                            highlight_color="#22C55E", font_color="#FFFFFF") is True
        content = out.read_text(encoding="utf-8-sig")
        # One dialogue event per word, highlight moves through the block
        assert content.count("Dialogue:") == 3
        assert content.count("\\c&H5EC522&") == 3  # #22C55E -> BGR 5EC522
        assert content.count("{\\r}") == 3          # reset to dimmed base style
        assert "Style: Default,Verdana," in content

    def test_karaoke_merges_fragments_too(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" YouTube", 0.0, 0.5), _w("-Kanal.", 0.5, 0.9)]
        assert generate_ass(self._transcript(words), 0, 10, str(out)) is True
        content = out.read_text(encoding="utf-8-sig")
        assert "YouTube-Kanal." in content
        assert content.count("Dialogue:") == 1

    def test_invalid_highlight_falls_back(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" test", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out),
                            highlight_color="#NOPE!!") is True
        content = out.read_text(encoding="utf-8-sig")
        assert "\\c&H00D7FF&" in content  # falls back to gold #FFD700

    def test_empty_range_returns_false(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" spaet", 50.0, 50.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out)) is False

    def test_ass_injection_neutralized(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" {\\b1}evil", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out)) is True
        content = out.read_text(encoding="utf-8-sig")
        assert "{\\b1}evil" not in content

    def test_glow_effect_tags(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" neon", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out),
                            effect="glow", highlight_color="#00FF88") is True
        content = out.read_text(encoding="utf-8-sig")
        assert "\\blur4" in content
        assert "\\3c&H88FF00&" in content  # glow outline in highlight color

    def test_pop_effect_animates_scale(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" pop", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out), effect="pop") is True
        content = out.read_text(encoding="utf-8-sig")
        assert "\\t(0,120,\\fscx112\\fscy112)" in content

    def test_bounce_effect_overshoots_and_settles(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" bounce", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out), effect="bounce") is True
        content = out.read_text(encoding="utf-8-sig")
        assert "\\fscx85\\fscy85" in content  # starts slightly small
        assert "\\t(0,90,\\fscx108\\fscy108)" in content  # gentle overshoot
        assert "\\t(90,180,\\fscx100\\fscy100)" in content  # settles at 100%

    def test_short_bounce_word_only_highlights_without_scale_restart(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" kurz", 0.0, 0.08), _w(" lang", 0.08, 0.50)]
        assert generate_ass(self._transcript(words), 0, 10, str(out), effect="bounce") is True
        events = [line for line in out.read_text(encoding="utf-8-sig").splitlines()
                  if line.startswith("Dialogue:")]
        assert "\\fscx" not in events[0]
        assert "\\fscx85" in events[1]

    def test_short_pop_word_only_highlights(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" kurz", 0.0, 0.08)]
        assert generate_ass(self._transcript(words), 0, 10, str(out), effect="pop") is True
        assert "\\fscx" not in out.read_text(encoding="utf-8-sig")

    def test_adjacent_events_share_exact_formatted_boundary(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" eins", 0.0, 0.333), _w(" zwei", 0.333, 0.667)]
        assert generate_ass(self._transcript(words), 0, 10, str(out)) is True
        events = [line.split(",") for line in out.read_text(encoding="utf-8-sig").splitlines()
                  if line.startswith("Dialogue:")]
        assert events[0][2] == events[1][1]

    def test_overlapping_whisper_words_cannot_overlap_across_blocks(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [
            _w(" ersteslangeswort", 0.0, 0.60),
            _w(" zweiteslangeswort", 0.50, 1.00),
        ]
        assert generate_ass(self._transcript(words), 0, 10, str(out), max_chars=10) is True
        events = [line.split(",") for line in out.read_text(encoding="utf-8-sig").splitlines()
                  if line.startswith("Dialogue:")]
        assert events[0][2] == events[1][1] == "0:00:00.50"

    def test_uppercase_transform(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" hallo", 0.0, 0.5), _w(" welt", 0.5, 1.0)]
        assert generate_ass(self._transcript(words), 0, 10, str(out), uppercase=True) is True
        content = out.read_text(encoding="utf-8-sig")
        assert "HALLO" in content and "WELT" in content
        assert "hallo" not in content.split("[Events]")[1]

    def test_base_opacity_dims_style_color(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" dim", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out),
                            font_color="#FFFFFF", base_opacity=0.4) is True
        content = out.read_text(encoding="utf-8-sig")
        # Dimming is fully-opaque scaled RGB (alpha would blend with the black
        # outline into muddy grey): factor 0.5 + 0.5*0.4 = 0.7 -> 0xB2
        assert "&H00B2B2B2" in content
        # no alpha-based dimming anywhere
        assert "\\1a" not in content

    def test_full_opacity_keeps_color_unchanged(self, tmp_path):
        from subtitles import generate_ass
        out = tmp_path / "subs.ass"
        words = [_w(" voll", 0.0, 0.5)]
        assert generate_ass(self._transcript(words), 0, 10, str(out),
                            font_color="#FFFFFF", base_opacity=1.0) is True
        content = out.read_text(encoding="utf-8-sig")
        assert "&H00FFFFFF" in content  # pure white, no dimming


class TestBuildSubtitleFilterAlignment:
    """ASS v4.00+ numpad alignment: 2=bottom, 5=middle, 8=top center.

    'top' used to map to 6 (middle right in v4.00+), pinning subtitles to the
    right edge — keep both burn paths on the same numpad codes.
    """

    def test_top_maps_to_numpad_top_center(self):
        from subtitles import build_subtitle_filter
        assert "Alignment=8" in build_subtitle_filter("subs.srt", alignment="top")

    def test_middle_and_bottom_mappings(self):
        from subtitles import build_subtitle_filter
        assert "Alignment=5" in build_subtitle_filter("subs.srt", alignment="middle")
        assert "Alignment=2" in build_subtitle_filter("subs.srt", alignment="bottom")

    def test_unknown_alignment_defaults_to_bottom(self):
        from subtitles import build_subtitle_filter
        assert "Alignment=2" in build_subtitle_filter("subs.srt", alignment="diagonal")
