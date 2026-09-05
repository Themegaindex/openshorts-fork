from types import SimpleNamespace

import gemini_worker
import pytest


def test_score_prompt_requires_a_score_for_every_window():
    assert "Score every candidate window" in gemini_worker.SCORE_PROMPT_TEMPLATE
    assert "exactly one entry for every input window" in gemini_worker.SCORE_PROMPT_TEMPLATE
    assert "up to 3 windows" not in gemini_worker.SCORE_PROMPT_TEMPLATE


def test_empty_response_diagnostics_preserve_block_and_finish_reasons():
    response = SimpleNamespace(
        prompt_feedback=SimpleNamespace(
            block_reason="SAFETY",
            block_reason_message="blocked test payload",
        ),
        candidates=[
            SimpleNamespace(
                finish_reason="SAFETY",
                finish_message="candidate stopped",
                safety_ratings=[
                    SimpleNamespace(
                        category="HARM_CATEGORY_DANGEROUS_CONTENT",
                        probability="MEDIUM",
                        blocked=True,
                    )
                ],
            )
        ],
    )

    diagnostics = gemini_worker._response_diagnostics(response)

    assert diagnostics["prompt_feedback"]["block_reason"] == "SAFETY"
    assert diagnostics["candidates"][0]["finish_reason"] == "SAFETY"
    assert diagnostics["candidates"][0]["safety_ratings"][0]["blocked"] is True


def test_response_diagnostics_tolerate_missing_optional_fields():
    diagnostics = gemini_worker._response_diagnostics(SimpleNamespace(candidates=[]))
    assert diagnostics == {
        "prompt_feedback": {"block_reason": None, "block_reason_message": None},
        "candidates": [],
    }


@pytest.mark.parametrize("payload", ["[]", '[{"viable": false}]', '"text"', "42"])
def test_json_response_parser_rejects_non_object_roots(payload):
    with pytest.raises(ValueError, match="root must be an object"):
        gemini_worker._parse_json_response_text(payload)


def test_longform_v2_uses_unit_id_schema_and_low_thinking(monkeypatch):
    monkeypatch.delenv("GEMINI_THINKING_LONGFORM", raising=False)

    config = gemini_worker._config_for_strategy(
        "structured-schema", "longform_plan_v2", "gemini-3.1-flash-lite",
    )

    assert config.response_schema is gemini_worker.LongformPlanV2Response
    assert config.temperature == 0.2
    assert str(config.thinking_config.thinking_level).lower().endswith("low")


def test_longform_v2_prompt_forbids_free_timestamps_and_padding():
    prompt = gemini_worker.LONGFORM_PLAN_V2_PROMPT_TEMPLATE.format(
        video_duration=6200,
        language="de",
        target_min_seconds=240,
        target_max_seconds=600,
        min_segment_seconds=20,
        max_segment_seconds=240,
        max_segments=12,
        max_chapters=6,
        min_chapters=2,
        blocks_json='[{"id":"block_001","units":[{"id":"u000001"}]}]',
        **gemini_worker.longform_plan_rules({"min_chapters": 2}),
    )

    assert "Never invent\n  timestamps" in prompt
    assert "Never\n  pad to ten minutes" in prompt
    assert '"start_unit_id": "u000001"' in prompt
    assert "240 strong coherent seconds" in prompt

    # A one-topic compilation must be excluded by the prompt as well, not only
    # by the validator.
    assert "2-6 chapters" in prompt
    assert "2 distinct chapters are MANDATORY" in prompt
    assert "5-15 second highlight" in prompt


def test_longform_v2_prompt_honours_single_chapter_and_disabled_cold_open():
    # The prompt must not demand what the quality gate then accepts/rejects.
    rules = gemini_worker.longform_plan_rules({
        "min_chapters": 1, "cold_open_enabled": False, "cold_open_max_seconds": 10,
    })
    assert "MANDATORY" not in rules["chapter_rule"]
    assert "single strong chapter is acceptable" in rules["chapter_rule"]
    assert "Cold opens are disabled" in rules["cold_open_rules"]
    assert "cold_open:null" in rules["cold_open_rules"]

    rules = gemini_worker.longform_plan_rules({"cold_open_max_seconds": 10})
    assert "5-10 second highlight" in rules["cold_open_rules"]

def test_longform_review_schema_can_return_repaired_final_plan():
    response = gemini_worker.LongformReviewResponse(
        approved=True,
        overall_score=91,
        ending_complete=True,
        critical_issues=[],
        dropped_chapter_ids=["chapter_02"],
        boundary_reviews=[{
            "segment_id": "chapter_01_span_01",
            "opening_complete": True,
            "ending_complete": True,
            "continuation_needed": False,
            "issue": "",
        }],
        joins=[{
            "id": "cold_open->chapter_01_span_01",
            "score": 90,
            "context_complete": True,
            "issue": "",
        }],
        plan={
            "viable": True,
            "video_title": "Titel",
            "youtube_description": "Beschreibung",
            "recommended_duration_seconds": 300,
            "duration_reason": "Genug starkes Material.",
            "cold_open": None,
            "chapters": [{
                "id": "chapter_01",
                "title": "Thema",
                "topic": "Thema",
                "priority": 90,
                "reason": "Wichtig",
                "spans": [{
                    "id": "chapter_01_span_01",
                    "start_unit_id": "u000001",
                    "end_unit_id": "u000020",
                }],
            }],
        },
    )

    assert response.plan.chapters[0].spans[0].start_unit_id == "u000001"
    assert response.dropped_chapter_ids == ["chapter_02"]


def test_response_schema_rejects_malformed_nested_review_fields():
    payload = {
        "approved": True,
        "overall_score": 90,
        "ending_complete": True,
        "critical_issues": [],
        "dropped_chapter_ids": [],
        "boundary_reviews": [],
        "joins": 42,
        "plan": {
            "viable": False,
            "video_title": "Titel",
            "youtube_description": "",
            "recommended_duration_seconds": 0,
            "duration_reason": "Nicht geeignet",
            "cold_open": None,
            "chapters": [],
        },
    }

    with pytest.raises(Exception, match="joins"):
        gemini_worker._validate_response_payload("longform_review", payload)


def test_review_prompt_requires_repair_before_hard_rejection():
    prompt = gemini_worker.LONGFORM_REVIEW_PROMPT_TEMPLATE.format(
        language="de",
        target_min_seconds=240,
        target_max_seconds=600,
        min_segment_seconds=20,
        max_segment_seconds=240,
        max_segments=12,
        max_chapters=6,
        min_chapters=2,
        repair_required="true",
        final_verification="false",
        repair_feedback_json='["weak_join"]',
        draft_plan_json="{}",
        review_context_json="{}",
    )

    assert "REPAIR_REQUIRED: true" in prompt
    assert "drop a chapter" in prompt
    assert "Score the FINAL joins" in prompt
    # Dropping chapters must not be able to collapse the edit to one topic;
    # the review is told how to repair that instead.
    assert "at least 2 chapters" in prompt
    assert "too_few_chapters" in prompt
    assert "regroup the EXISTING spans" in prompt
    assert "Audit EVERY segment separately" in prompt
    assert "current_start_unit_id" in prompt
    assert "current_end_unit_id" in prompt
    assert "FINAL_VERIFICATION_ONLY: false" in prompt


def test_review_prompt_makes_post_repair_verification_read_only():
    prompt = gemini_worker.LONGFORM_REVIEW_PROMPT_TEMPLATE.format(
        language="de",
        target_min_seconds=240,
        target_max_seconds=600,
        min_segment_seconds=20,
        max_segment_seconds=240,
        max_segments=12,
        max_chapters=6,
        min_chapters=2,
        repair_required="false",
        final_verification="true",
        repair_feedback_json="[]",
        draft_plan_json="{}",
        review_context_json="{}",
    )

    assert "FINAL_VERIFICATION_ONLY: true" in prompt
    assert "do not alter IDs, boundaries, order, chapters" in prompt
    assert "continuation_needed:true" in prompt
