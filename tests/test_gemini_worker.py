from types import SimpleNamespace

import gemini_worker


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
