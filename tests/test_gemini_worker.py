from types import SimpleNamespace

import gemini_worker


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


def test_longform_strategy_uses_its_schema_and_conservative_temperature():
    config = gemini_worker._config_for_strategy(
        "structured-schema", "longform_plan", "gemini-3-flash-preview",
    )

    assert config.response_schema is gemini_worker.LongformPlanResponse
    assert config.temperature == 0.4


def test_longform_prompt_formats_story_and_duration_contract():
    prompt = gemini_worker.LONGFORM_PLAN_PROMPT_TEMPLATE.format(
        video_duration=1200,
        language="de",
        windows_json='[{"id":"window_001"}]',
        target_min_seconds=480,
        target_max_seconds=600,
        min_segment_seconds=20,
        max_segment_seconds=240,
    )

    assert "480 to\n600 seconds" in prompt
    assert "setup, bridge, body or payoff" in prompt
    assert '"required": <true or false>' in prompt
    assert '"id":"window_001"' in prompt


def test_longform_schema_carries_continuity_fields():
    response = gemini_worker.LongformPlanResponse(
        viable=True,
        video_title="Title",
        youtube_description="Description",
        segments=[{
            "start": 10,
            "end": 80,
            "chapter_title": "Setup",
            "priority": 70,
            "continuity_importance": 95,
            "required": True,
            "role": "setup",
            "reason": "Needed context",
        }],
    )

    assert response.segments[0].required is True
    assert response.segments[0].continuity_importance == 95
