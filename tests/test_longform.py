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
    assert "afade=t=out:st=29.990:d=0.010" in command[command.index("-af") + 1]


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


def _v2_unit(index, start, end, text, *, boundary="sentence", start_boundary="sentence"):
    return {
        "id": f"u{index:06d}",
        "start": start,
        "end": end,
        "cut_start": start,
        "cut_end": end,
        "start_cut_window": [start, start],
        "end_cut_window": [end, end],
        "text": text,
        "start_boundary": start_boundary,
        "end_boundary": boundary,
        "boundary_confidence": "relaxed" if boundary == "best_pause" else "high",
    }


def _v2_plan(*, replay=False, cold_start="u000006", cold_end="u000006", second_chapter=False):
    return {
        "viable": True,
        "video_title": "Smart edit",
        "youtube_description": "A deliberately edited result.",
        "recommended_duration_seconds": 250,
        "duration_reason": "Four strong minutes were available.",
        "cold_open": {
            "id": "cold_open",
            "start_unit_id": cold_start,
            "end_unit_id": cold_end,
            "title": "Cold Open",
            "priority": 100,
            "reason": "Strong standalone hook",
            "replay_in_body": replay,
        },
        "chapters": [{
            "id": "chapter_01",
            "title": "Thema",
            "topic": "Thema",
            "priority": 90,
            "reason": "Strong topic",
            "spans": [{
                "id": "chapter_01_span_01",
                "start_unit_id": "u000001",
                "end_unit_id": "u000004",
            }],
        }] + ([{
            "id": "chapter_02",
            "title": "Zweites Thema",
            "topic": "Zweites Thema",
            "priority": 80,
            "reason": "Second distinct topic",
            "spans": [{
                "id": "chapter_02_span_01",
                "start_unit_id": "u000005",
                "end_unit_id": "u000005",
            }],
        }] if second_chapter else []),
    }


def test_editorial_units_prefer_complete_sentences_and_have_fast_speech_fallback():
    transcript = {
        "segments": [{
            "start": 0,
            "end": 8,
            "text": "Das ist, noch vollständig. schneller sprecher ohne punkt weiter weiter weiter",
            "words": [
                {"word": " Das", "start": 0.0, "end": 0.2},
                {"word": " ist,", "start": 0.2, "end": 0.4},
                {"word": " noch", "start": 0.45, "end": 0.7},
                {"word": " vollständig.", "start": 0.7, "end": 1.0},
                {"word": " schneller", "start": 1.7, "end": 2.2},
                {"word": " sprecher", "start": 2.2, "end": 2.7},
                {"word": " ohne", "start": 2.7, "end": 3.2},
                {"word": " punkt", "start": 3.2, "end": 3.7},
                {"word": " weiter", "start": 3.7, "end": 4.2},
                {"word": " weiter", "start": 4.2, "end": 4.7},
                {"word": " weiter", "start": 4.7, "end": 5.2},
            ],
        }],
    }

    units = longform.build_editorial_units(
        transcript,
        5.2,
        strong_pause_seconds=0.55,
        fallback_window_seconds=2.0,
        min_unit_seconds=0.5,
    )

    assert units[0]["text"].endswith("vollständig.")
    assert units[0]["end_boundary"] == "sentence"
    assert any(item["end_boundary"] == "best_pause" for item in units[1:])
    assert all(item["end"] - item["start"] < 3.0 for item in units[1:])


def test_editorial_units_handle_unspaced_tokens_and_timestamp_jitter():
    transcript = {
        "segments": [{
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.6},
                {"word": "world.", "start": 0.59, "end": 1.0},
                {"word": "Next", "start": 0.99, "end": 1.4},
                {"word": "thought.", "start": 1.4, "end": 2.0},
            ],
        }],
    }

    units = longform.build_editorial_units(transcript, 2.0)

    assert units[0]["text"] == "Hello world."
    assert units[0]["end_cut_window"][0] <= units[0]["end_cut_window"][1]
    assert units[1]["start_cut_window"][0] <= units[1]["start_cut_window"][1]
    assert units[1]["cut_start"] <= units[1]["start"]


def test_transcript_fingerprint_includes_resolved_cut_boundaries():
    units = [_v2_unit(1, 0, 30, "Vollständiger Satz.")]
    original = longform.transcript_fingerprint(units)
    units[0]["cut_end"] = 29.8

    assert longform.transcript_fingerprint(units) != original


def test_v2_plan_resolves_only_stable_unit_ids_and_adaptive_duration():
    units = [
        _v2_unit(1, 0, 60, "Erste vollständige Antwort."),
        _v2_unit(2, 60, 120, "Zweite vollständige Antwort."),
        _v2_unit(3, 120, 180, "Dritte vollständige Antwort."),
        _v2_unit(4, 180, 240, "Vierte vollständige Antwort."),
        _v2_unit(5, 240, 300, "Weiterer Kontext."),
        _v2_unit(6, 300, 310, "Das ist der starke Hook."),
    ]

    resolved = longform.resolve_unit_plan(_v2_plan(second_chapter=True), units)

    assert resolved["planner_version"] == 2
    assert resolved["total_duration"] == 310
    assert resolved["validation_issues"] == []
    assert resolved["segments"][0]["role"] == "cold_open"
    assert resolved["segments"][1]["unit_start_id"] == "u000001"
    assert resolved["segments"][1]["unit_end_id"] == "u000004"
    assert resolved["segments"][2]["chapter_id"] == "chapter_02"


def test_model_chapters_are_sorted_by_source_without_dropping_content():
    units = [
        _v2_unit(index, (index - 1) * 60, index * 60, f"Satz {index}.")
        for index in range(1, 7)
    ]
    plan = _v2_plan(second_chapter=True)
    plan["chapters"] = list(reversed(plan["chapters"]))

    ordered, protected = longform.order_unit_plan_chronologically(plan, units)
    resolved = longform.resolve_unit_plan(ordered, units)

    assert [chapter["id"] for chapter in ordered["chapters"]] == ["chapter_01", "chapter_02"]
    assert protected == {"chapter_01", "chapter_02"}
    assert len(ordered["chapters"]) == len(plan["chapters"])
    assert not any(issue.startswith("non_chronological_or_overlapping:") for issue in resolved["validation_issues"])


def test_v2_single_chapter_plan_is_rejected_but_stays_repairable():
    """One chapter is a one-topic compilation, not the planned best-of edit.

    The issue must be a validation finding (repairable by regrouping the same
    spans in the review pass), not a hard resolve error.
    """
    units = [
        _v2_unit(1, 0, 60, "Eins."),
        _v2_unit(2, 60, 120, "Zwei."),
        _v2_unit(3, 120, 180, "Drei."),
        _v2_unit(4, 180, 240, "Vier."),
        _v2_unit(5, 240, 300, "Fünf."),
        _v2_unit(6, 300, 310, "Hook."),
    ]

    single = longform.resolve_unit_plan(_v2_plan(), units)
    assert "too_few_chapters" in single["validation_issues"]
    # A perfect self-review cannot wave the structural defect through.
    flawless_review = {
        "approved": True,
        "overall_score": 100,
        "ending_complete": True,
        "critical_issues": [],
        "joins": [{"id": "cold_open->chapter_01_span_01", "score": 100, "context_complete": True}],
    }
    assert "too_few_chapters" in longform.assess_editorial_quality(single, flawless_review)

    two = longform.resolve_unit_plan(_v2_plan(second_chapter=True), units)
    assert "too_few_chapters" not in two["validation_issues"]

    # Genuinely single-topic sources stay possible via configuration.
    relaxed = longform.resolve_unit_plan(_v2_plan(), units, min_chapters=1)
    assert "too_few_chapters" not in relaxed["validation_issues"]


def test_v2_empty_chapter_does_not_satisfy_the_minimum():
    units = [_v2_unit(index, (index - 1) * 60, index * 60, f"Satz {index}.") for index in range(1, 7)]
    plan = _v2_plan()
    plan["chapters"].append({
        "id": "chapter_02", "title": "Leer", "topic": "Leer",
        "priority": 50, "reason": "", "spans": [],
    })

    resolved = longform.resolve_unit_plan(plan, units)

    assert "empty_chapter:chapter_02" in resolved["validation_issues"]
    assert "too_few_chapters" in resolved["validation_issues"]


def test_v2_cold_open_overlap_requires_explicit_context_replay():
    units = [
        _v2_unit(1, 0, 60, "Eins."),
        _v2_unit(2, 60, 120, "Zwei."),
        _v2_unit(3, 120, 180, "Drei."),
        _v2_unit(4, 180, 240, "Vier."),
        _v2_unit(5, 240, 300, "Fünf."),
        _v2_unit(6, 300, 310, "Hook."),
    ]
    plan = _v2_plan(cold_start="u000002", cold_end="u000002", replay=False)

    unresolved_duplicate = longform.resolve_unit_plan(plan, units)
    assert "cold_open_overlap_without_replay" in unresolved_duplicate["validation_issues"]

    plan["cold_open"]["replay_in_body"] = True
    explicit_replay = longform.resolve_unit_plan(plan, units)
    assert "cold_open_overlap_without_replay" not in explicit_replay["validation_issues"]


def test_v2_regression_rejects_obviously_unfinished_comma_or_dass_endings():
    units = [
        _v2_unit(1, 0, 60, "Eins."),
        _v2_unit(2, 60, 120, "Zwei."),
        _v2_unit(3, 120, 180, "Drei."),
        _v2_unit(4, 180, 240, "Weißt du, wo das aufgefallen ist, dass"),
        _v2_unit(5, 240, 300, "Fünf."),
        _v2_unit(6, 300, 310, "Das war die Zeit,"),
    ]

    resolved = longform.resolve_unit_plan(_v2_plan(), units)

    assert "obviously_incomplete_end:chapter_01_span_01" in resolved["validation_issues"]
    assert "obviously_incomplete_end:cold_open" in resolved["validation_issues"]


def test_actual_regression_excerpt_keeps_dass_attached_to_its_completion():
    # Real word timings around the old job's 6044.52s cut. V1 ended exactly on
    # "dass"; V2 must expose a complete selectable unit instead.
    transcript = {"segments": [{"words": [
        {"word": " Weißt", "start": 6038.14, "end": 6038.34},
        {"word": " du,", "start": 6038.34, "end": 6038.38},
        {"word": " wo", "start": 6038.44, "end": 6038.46},
        {"word": " das", "start": 6038.46, "end": 6038.58},
        {"word": " richtig", "start": 6038.58, "end": 6038.74},
        {"word": " aufgefallen", "start": 6038.74, "end": 6039.24},
        {"word": " ist,", "start": 6039.24, "end": 6039.36},
        {"word": " dass", "start": 6041.76, "end": 6044.52},
        {"word": " den", "start": 6044.52, "end": 6044.66},
        {"word": " Marsch", "start": 6044.66, "end": 6046.24},
        {"word": " in", "start": 6046.24, "end": 6046.36},
        {"word": " Berlin.", "start": 6046.36, "end": 6046.60},
    ]}]}

    units = longform.build_editorial_units(transcript, 6046.60)

    assert not any(item["text"].rstrip().endswith("dass") for item in units)
    dass_unit = next(item for item in units if "dass" in item["text"].split())
    assert dass_unit["text"].endswith("Berlin.")
    assert dass_unit["text"].startswith("dass")
    checked = longform.resolve_unit_plan(
        {
            "viable": True,
            "video_title": "Regression",
            "youtube_description": "",
            "recommended_duration_seconds": 5,
            "duration_reason": "Regression",
            "cold_open": None,
            "chapters": [{
                "id": "chapter_01",
                "title": "Thema",
                "topic": "Thema",
                "priority": 80,
                "reason": "Regression",
                "spans": [{
                    "id": "span_01",
                    "start_unit_id": dass_unit["id"],
                    "end_unit_id": dass_unit["id"],
                }],
            }],
        },
        units,
        min_output_seconds=0,
        max_output_seconds=30,
        min_segment_seconds=0,
        cold_open_enabled=False,
    )
    assert "obviously_incomplete_start:span_01" in checked["validation_issues"]


def test_quality_gate_combines_invariants_and_review_scores():
    units = [
        _v2_unit(1, 0, 60, "Eins."),
        _v2_unit(2, 60, 120, "Zwei."),
        _v2_unit(3, 120, 180, "Drei."),
        _v2_unit(4, 180, 240, "Vier."),
        _v2_unit(5, 240, 300, "Fünf."),
        _v2_unit(6, 300, 310, "Hook."),
    ]
    plan = longform.resolve_unit_plan(_v2_plan(second_chapter=True), units)
    approved_review = {
        "approved": True,
        "overall_score": 90,
        "ending_complete": True,
        "critical_issues": [],
        "boundary_reviews": [
            {
                "segment_id": segment["segment_id"],
                "opening_complete": True,
                "ending_complete": True,
                "continuation_needed": False,
                "issue": "",
            }
            for segment in plan["segments"]
        ],
        "joins": [
            {"id": "cold_open->chapter_01_span_01", "score": 88, "context_complete": True},
            {"id": "chapter_01_span_01->chapter_02_span_01", "score": 88, "context_complete": True},
        ],
    }

    assert longform.assess_editorial_quality(plan, approved_review) == []

    rejected = dict(approved_review)
    rejected["overall_score"] = 70
    rejected["ending_complete"] = False
    assert set(longform.assess_editorial_quality(plan, rejected)) >= {
        "overall_review_score_below_threshold",
        "incomplete_ending",
    }

    missing_boundary = dict(approved_review)
    missing_boundary["boundary_reviews"] = approved_review["boundary_reviews"][:-1]
    assert "missing_boundary_review:chapter_02_span_01" in longform.assess_editorial_quality(
        plan,
        missing_boundary,
    )

    unfinished_boundary = dict(approved_review)
    unfinished_boundary["boundary_reviews"] = [dict(item) for item in approved_review["boundary_reviews"]]
    unfinished_boundary["boundary_reviews"][-1]["continuation_needed"] = True
    assert "incomplete_segment_ending:chapter_02_span_01" in longform.assess_editorial_quality(
        plan,
        unfinished_boundary,
    )

    assert "avoidable_chapter_drop:chapter_03" in longform.assess_editorial_quality(
        plan,
        approved_review,
        protected_chapter_ids={"chapter_03"},
    )


def test_review_context_exposes_only_selected_ranges_and_nearby_units():
    units = [_v2_unit(index, (index - 1) * 60, index * 60, f"Satz {index}.") for index in range(1, 8)]
    resolved = longform.resolve_unit_plan(_v2_plan(), units)

    context = longform.build_review_context(resolved, units, neighbor_units=1)

    assert [item["segment_id"] for item in context["assembled_segments"]] == [
        "cold_open", "chapter_01_span_01",
    ]
    assert context["expected_joins"][0]["id"] == "cold_open->chapter_01_span_01"
    exposed = {
        unit["id"]
        for neighborhood in context["boundary_neighborhoods"]
        for candidate_key in ("start_candidate_units", "end_candidate_units")
        for unit in neighborhood[candidate_key]
    }
    assert "u000001" in exposed
    assert "u000007" in exposed


def test_review_context_expands_by_time_beyond_four_fast_speaker_units():
    units = [
        _v2_unit(index, (index - 1) * 5, index * 5, f"Satz {index}.")
        for index in range(1, 41)
    ]
    plan = {
        "viable": True,
        "video_title": "Grenztest",
        "youtube_description": "",
        "recommended_duration_seconds": 30,
        "duration_reason": "Test",
        "cold_open": None,
        "chapters": [{
            "id": "chapter_01",
            "title": "Thema",
            "topic": "Thema",
            "priority": 90,
            "reason": "Test",
            "spans": [{
                "id": "span_01",
                "start_unit_id": "u000010",
                "end_unit_id": "u000015",
            }],
        }],
    }
    resolved = longform.resolve_unit_plan(
        plan,
        units,
        min_output_seconds=0,
        max_output_seconds=300,
        min_segment_seconds=0,
        min_chapters=1,
        cold_open_enabled=False,
    )

    context = longform.build_review_context(
        resolved,
        units,
        neighbor_units=4,
        neighbor_seconds=75,
        max_neighbor_units=24,
    )
    neighborhood = context["boundary_neighborhoods"][0]

    assert "u000001" in {item["id"] for item in neighborhood["start_candidate_units"]}
    assert "u000030" in {item["id"] for item in neighborhood["end_candidate_units"]}
    assert len(neighborhood["start_candidate_units"]) <= 24
    assert len(neighborhood["end_candidate_units"]) <= 24
    assert "before_start_units" not in neighborhood
    assert "after_end_units" not in neighborhood


def test_real_regression_opening_reference_is_rejected_deterministically():
    units = [
        _v2_unit(1, 0, 30, "Damit sie da?"),
        _v2_unit(2, 30, 120, "Die vollständige Erklärung folgt."),
        _v2_unit(3, 120, 240, "Das Thema endet vollständig."),
    ]
    plan = {
        "viable": True,
        "video_title": "Regression",
        "youtube_description": "",
        "recommended_duration_seconds": 240,
        "duration_reason": "Test",
        "cold_open": None,
        "chapters": [{
            "id": "chapter_01",
            "title": "Thema",
            "topic": "Thema",
            "priority": 90,
            "reason": "Test",
            "spans": [{
                "id": "span_01",
                "start_unit_id": "u000001",
                "end_unit_id": "u000003",
            }],
        }],
    }

    resolved = longform.resolve_unit_plan(plan, units, min_chapters=1, cold_open_enabled=False)

    assert "obviously_incomplete_start:span_01" in resolved["validation_issues"]


def test_reviewed_span_cannot_jump_into_another_segments_neighborhood():
    units = [
        _v2_unit(index, (index - 1) * 20, index * 20, f"Satz {index}.")
        for index in range(1, 21)
    ]
    plan = _v2_plan(cold_start="u000020", cold_end="u000020")
    plan["chapters"][0]["spans"].append({
        "id": "chapter_01_span_02",
        "start_unit_id": "u000010",
        "end_unit_id": "u000012",
    })
    resolved = longform.resolve_unit_plan(
        plan,
        units,
        min_output_seconds=0,
        max_output_seconds=600,
    )
    context = longform.build_review_context(resolved, units, neighbor_units=1)
    allowed_by_segment = {
        item["segment_id"]: {
            "start": {unit["id"] for unit in item["start_candidate_units"]},
            "end": {unit["id"] for unit in item["end_candidate_units"]},
        }
        for item in context["boundary_neighborhoods"]
    }
    reviewed = _v2_plan(cold_start="u000020", cold_end="u000020")
    reviewed["chapters"][0]["spans"][0]["end_unit_id"] = "u000012"

    checked = longform.resolve_unit_plan(
        reviewed,
        units,
        min_output_seconds=0,
        max_output_seconds=600,
        allowed_unit_ids_by_segment=allowed_by_segment,
    )

    assert "unit_outside_own_review_context:chapter_01_span_01" in checked["validation_issues"]
