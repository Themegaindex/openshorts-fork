import math

import pytest

import longform


def _segment(start, end, title, *, role="body", priority=50,
             continuity=50, required=False):
    return {
        "start": start,
        "end": end,
        "chapter_title": title,
        "role": role,
        "priority": priority,
        "continuity_importance": continuity,
        "required": required,
    }


def test_normalize_pins_one_cold_open_and_keeps_its_overlap_with_body():
    payload = {
        "viable": True,
        "video_title": "A coherent cut",
        "youtube_description": "Description",
        "segments": [
            _segment(100, 112, "Teaser", role="cold_open", priority=90),
            _segment(102, 118, "Weaker teaser", role="cold_open", priority=40),
            _segment(90, 130, "Setup", role="setup", required=True),
            _segment(200, 250, "Payoff", role="payoff", required=True),
        ],
    }

    result = longform.normalize_longform_plan(
        payload, 300, target_min_seconds=60, target_max_seconds=180,
        min_segment_seconds=20,
    )

    assert result["viable"] is True
    assert [item["role"] for item in result["segments"]] == ["cold_open", "setup", "payoff"]
    assert result["segments"][0]["start"] == 100
    assert result["segments"][1]["start"] == 90
    assert result["segments"][0]["end"] > result["segments"][1]["start"]


def test_normalize_rejects_a_plan_that_misses_its_adaptive_minimum():
    result = longform.normalize_longform_plan(
        {
            "viable": True,
            "video_title": "Too short",
            "youtube_description": "",
            "segments": [_segment(0, 220, "Only section", role="setup", required=True)],
        },
        900,
        target_min_seconds=480,
        target_max_seconds=600,
        min_segment_seconds=20,
    )

    assert result["total_duration"] == 220
    assert result["viable"] is False
    assert "short_result" in result["warnings"]


def test_merge_absorbs_small_gap_and_preserves_continuity_metadata():
    merged = longform.merge_plan_segments(
        [
            _segment(0, 30, "Setup", priority=60, continuity=80),
            _segment(33, 70, "Bridge", role="bridge", priority=75, required=True),
        ],
        merge_gap_seconds=4,
    )

    assert len(merged) == 1
    assert merged[0]["start"] == 0
    assert merged[0]["end"] == 70
    assert merged[0]["priority"] == 75
    assert merged[0]["continuity_importance"] == 80
    assert merged[0]["required"] is True


@pytest.mark.parametrize("duration", [241, 250, 259, 481])
def test_split_long_segment_rebalances_short_tails_within_duration_bounds(duration):
    pieces = longform._split_long_segment(
        _segment(0, duration, "Long section"),
        240,
        min_segment_seconds=20,
        words=[],
    )

    assert pieces[0]["start"] == 0
    assert pieces[-1]["end"] == duration
    assert all(
        current["end"] == following["start"]
        for current, following in zip(pieces, pieces[1:])
    )
    assert all(20 <= item["end"] - item["start"] <= 240 for item in pieces)
    assert math.isclose(sum(item["end"] - item["start"] for item in pieces), duration)


def test_split_long_segment_rejects_impossible_duration_constraints():
    with pytest.raises(ValueError, match="minimum and maximum"):
        longform._split_long_segment(
            _segment(0, 31, "Impossible section"),
            30,
            min_segment_seconds=20,
            words=[],
        )


def test_fit_drops_optional_material_but_protects_story_roles():
    segments = [
        _segment(0, 10, "Teaser", role="cold_open", priority=100),
        _segment(10, 110, "Setup", role="setup", priority=70),
        _segment(120, 300, "Aside", priority=10, continuity=5),
        _segment(310, 410, "Bridge", role="bridge", priority=55),
        _segment(420, 520, "Payoff", role="payoff", priority=95),
    ]

    fitted, warnings = longform.fit_plan_to_target(segments, 300, 350)

    assert sum(item["end"] - item["start"] for item in fitted) == 310
    assert {item["role"] for item in fitted} >= {"cold_open", "setup", "bridge", "payoff"}
    assert all(item["chapter_title"] != "Aside" for item in fitted)
    assert any(item.startswith("dropped_optional_segment:") for item in warnings)


def test_fit_trims_at_sentence_when_whole_segment_drop_would_undershoot():
    segments = [
        _segment(0, 130, "Setup", role="setup"),
        _segment(130, 230, "Detail", priority=10, continuity=10),
        _segment(230, 360, "Payoff", role="payoff"),
    ]
    words = [
        {"w": "one.", "s": 159, "e": 160},
        {"w": "two.", "s": 169, "e": 170},
        {"w": "three.", "s": 199, "e": 200},
    ]

    fitted, warnings = longform.fit_plan_to_target(
        segments, 300, 300, words=words, min_segment_seconds=20,
    )

    assert math.isclose(sum(item["end"] - item["start"] for item in fitted), 300)
    assert fitted[1]["end"] == 170
    assert "trimmed_at_sentence:Detail" in warnings


def test_fit_ignores_sentence_boundary_outside_remaining_cut_budget():
    segments = [_segment(0, 610, "Story", role="setup", required=True)]
    words = [
        {"w": "old sentence.", "s": 29, "e": 30},
        {"w": "continuing", "s": 599, "e": 600},
    ]

    fitted, warnings = longform.fit_plan_to_target(
        segments, 480, 600, words=words, min_segment_seconds=20,
    )

    assert sum(item["end"] - item["start"] for item in fitted) == 600
    assert fitted[0]["end"] == 600
    assert "short_result" not in warnings


def test_chapters_fold_short_cold_open_marker_into_valid_timeline():
    chapters = longform.build_chapters([
        _segment(100, 108, "Cold open", role="cold_open"),
        _segment(0, 80, "Context", role="setup"),
        _segment(100, 160, "Development", role="bridge"),
        _segment(200, 260, "Payoff", role="payoff"),
    ])

    assert chapters[0] == {"time_seconds": 0.0, "title": "Cold open", "formatted": "0:00"}
    # The first body starts at assembled 0:08, so its marker is intentionally folded.
    assert [item["formatted"] for item in chapters] == ["0:00", "1:28", "2:28"]
    assert all(
        chapters[index]["time_seconds"] - chapters[index - 1]["time_seconds"] >= 10
        for index in range(1, len(chapters))
    )


def test_normalize_warns_when_a_short_chapter_marker_is_folded():
    result = longform.normalize_longform_plan(
        {
            "viable": True,
            "video_title": "Story",
            "youtube_description": "",
            "segments": [
                _segment(100, 105, "Teaser", role="cold_open"),
                _segment(0, 20, "Setup", role="setup"),
                _segment(30, 70, "Payoff", role="payoff"),
            ],
        },
        120,
        target_min_seconds=60,
        target_max_seconds=120,
        min_segment_seconds=20,
    )

    assert result["viable"] is True
    assert "short_chapter_markers_folded" in result["warnings"]


def test_timestamp_and_description_formatting():
    assert longform.format_timestamp(65) == "1:05"
    assert longform.format_timestamp(3661) == "1:01:01"
    description = longform.build_youtube_description(
        "Watch the full story.",
        [{"formatted": "0:00", "title": "Intro"}, {"formatted": "1:10", "title": "Reveal"}],
    )
    assert description == "Watch the full story.\n\n0:00 Intro\n1:10 Reveal"


def test_segment_cut_uses_duration_and_optional_16_9_canvas():
    filter_graph = longform.blurred_16_9_filter()
    command = longform.segment_cut_command(
        "input.mp4", 12.5, 42.5, "output.mp4", filter_complex=filter_graph,
    )

    assert command[command.index("-ss") + 1] == "12.500"
    assert command[command.index("-t") + 1] == "30.000"
    assert "-to" not in command
    assert command[command.index("-filter_complex") + 1] == filter_graph
    assert command[command.index("-map") + 1] == "[video]"
    assert "afade=t=out:st=29.960:d=0.040" in command[command.index("-af") + 1]


def test_exact_16_9_cut_resets_sample_aspect_ratio():
    command = longform.segment_cut_command("input.mp4", 0, 30, "output.mp4")

    assert command[command.index("-vf") + 1] == "setsar=1"
    assert command[command.index("-map") + 1] == "0:v:0"


def test_odd_sized_16_9_source_uses_the_even_canvas():
    assert longform.needs_16_9_canvas(720, 405) is True
    assert longform.needs_16_9_canvas(1920, 1080) is False


def test_zero_audio_fade_cleanly_disables_the_filter():
    command = longform.segment_cut_command(
        "input.mp4", 0, 30, "output.mp4", fade_seconds=0,
    )

    assert "-af" not in command


def test_concat_manifest_escapes_quotes_and_command_stream_copies_video(tmp_path):
    filename = tmp_path / "speaker's segment.mp4"
    manifest = longform.concat_manifest_text([filename])
    assert "speaker'\\''s segment.mp4" in manifest

    command = longform.concat_command(tmp_path / "manifest.txt", tmp_path / "joined.mp4")
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"


def test_canvas_detection_leaves_real_16_9_sources_alone():
    assert longform.needs_16_9_canvas(1920, 1080) is False
    assert longform.needs_16_9_canvas(1080, 1920) is True
    assert longform.needs_16_9_canvas(1440, 1080) is True


def test_model_not_viable_is_preserved_for_score_fallback_decision():
    result = longform.normalize_longform_plan(
        {"viable": False, "video_title": "No story", "segments": []}, 900,
    )
    assert result["viable"] is False
    assert result["warnings"] == ["model_marked_not_viable"]
