import argparse
import json
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel

from clip_selection import lookup_model_prices

load_dotenv()


# --- Structured output schemas (passed as response_schema so the API
# --- guarantees the format instead of us repairing free-form JSON). ---

class ScoredWindowModel(BaseModel):
    id: str
    start: float
    end: float
    score: int
    reason: str


class ScoreResponse(BaseModel):
    windows: List[ScoredWindowModel]


class DetailClipModel(BaseModel):
    start: float
    end: float
    source_window_id: str
    predicted_score: int
    video_description_for_tiktok: str
    video_description_for_instagram: str
    video_title_for_youtube_short: str
    viral_hook_text: str


class DetailResponse(BaseModel):
    shorts: List[DetailClipModel]


class LongformUnitSpanModel(BaseModel):
    id: str
    start_unit_id: str
    end_unit_id: str


class LongformColdOpenModel(LongformUnitSpanModel):
    title: str
    priority: int
    reason: str
    replay_in_body: bool


class LongformChapterModel(BaseModel):
    id: str
    title: str
    topic: str
    priority: int
    reason: str
    spans: List[LongformUnitSpanModel]


class LongformPlanV2Response(BaseModel):
    viable: bool
    video_title: str
    youtube_description: str
    recommended_duration_seconds: int
    duration_reason: str
    cold_open: Optional[LongformColdOpenModel]
    chapters: List[LongformChapterModel]


class LongformJoinReviewModel(BaseModel):
    id: str
    score: int
    context_complete: bool
    issue: str


class LongformBoundaryReviewModel(BaseModel):
    segment_id: str
    opening_complete: bool
    ending_complete: bool
    continuation_needed: bool
    issue: str


class LongformReviewResponse(BaseModel):
    approved: bool
    overall_score: int
    ending_complete: bool
    critical_issues: List[str]
    dropped_chapter_ids: List[str]
    boundary_reviews: List[LongformBoundaryReviewModel]
    joins: List[LongformJoinReviewModel]
    plan: LongformPlanV2Response


_RESPONSE_SCHEMAS = {
    "detail": DetailResponse,
    "score": ScoreResponse,
    "longform_plan_v2": LongformPlanV2Response,
    "longform_review": LongformReviewResponse,
}


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _log(message: str) -> None:
    stream = sys.stdout
    text = str(message)
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text + "\n")
    stream.flush()

SCORE_PROMPT_TEMPLATE = """
You are a senior short-form video strategist.
Score every candidate window in this batch.

Rules:
- Return only valid JSON.
- Return exactly one entry for every input window, preserving its `id`, `start`,
  and `end`. Never omit weak windows; give them a low score instead.
- `score` must be an integer from 0 to 100.
- THE 2-SECOND TEST is the main criterion: would the first 2 seconds of this
  moment force a cold viewer (no context) to keep watching? Windows that only
  work with prior context score low.
- Prefer windows with strong hooks, conflict, surprise, outrage, emotion,
  novelty, big numbers, or a clear payoff.
- Ignore weak filler, housekeeping, outros, rambling transitions, and
  low-signal padding unless there is an obvious hook or payoff.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_DURATION_SECONDS: {video_duration}
WINDOWS_JSON:
{windows_json}

Return only:
{{
  "windows": [
    {{
      "id": "<window id>",
      "start": <number>,
      "end": <number>,
      "score": <integer 0-100>,
      "reason": "<very short reason>"
    }}
  ]
}}
"""

DETAIL_PROMPT_TEMPLATE = """
You are a senior short-form video editor and viral copywriter.
Choose the BEST short clips from these shortlisted candidate windows.

CLIP RULES:
- Return only valid JSON.
- Each clip must be 15 to 60 seconds long, in absolute seconds from the start of the source video.
- Stay within the candidate window boundaries.
- THE 2-SECOND RULE: the clip MUST open on its strongest moment. If the first
  2 seconds would not stop a cold viewer from scrolling, move the start or skip the clip.
- Start slightly before the hook and end slightly after the payoff when possible.
- Do not cut in the middle of a word or phrase.
- No generic intros/outros unless they are the hook.
- Prefer one great clip per candidate window. Maximum 2 clips per window only if clearly justified.
- DIVERSITY: never return two clips that make the same point, tell the same
  story, or land the same joke — even across different windows. Pick the
  stronger one and drop the other.

HOOK PLAYBOOK — pick the strongest fitting pattern for `viral_hook_text` (max 10 words):
- Open question: "Why does everyone get this wrong?"
- Hot take / controversy: "Stop doing this. Seriously."
- Number / fact shock: "97% of people miss this."
- Story loop: "This one email almost ruined me."
- POV / pattern interrupt: "POV: you finally understand it."
(These are English PATTERNS — always write the actual hook in TRANSCRIPT_LANGUAGE.)

COPY RULES — ALL text fields (descriptions, title, hook) MUST be written in TRANSCRIPT_LANGUAGE ({language}):
- Descriptions (TikTok + Instagram): 1-2 punchy sentences that tease the payoff
  without spoiling it, then 3-5 topically relevant hashtags. No generic hashtag spam.
- `video_title_for_youtube_short`: max 100 chars, curiosity-driven, no fake claims.
- `predicted_score`: honest 0-100 estimate of viral potential.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_DURATION_SECONDS: {video_duration}
CANDIDATE_WINDOWS_JSON:
{windows_json}

Return only:
{{
  "shorts": [
    {{
      "start": <number>,
      "end": <number>,
      "source_window_id": "<window id>",
      "predicted_score": <integer 0-100>,
      "video_description_for_tiktok": "<description + hashtags>",
      "video_description_for_instagram": "<description + hashtags>",
      "video_title_for_youtube_short": "<title max 100 chars>",
      "viral_hook_text": "<short overlay max 10 words>"
    }}
  ]
}}
"""


LONGFORM_PLAN_V2_PROMPT_TEMPLATE = """
You are the planning editor for a polished YouTube interview edit. Use the
existing importance scores to select the strongest DISTINCT topics, while
making every retained topic understandable on its own. This is a chronological
best-of edit, not a random compilation and not one artificially forced story.

NON-NEGOTIABLE CONTRACT:
- Return only valid JSON matching the requested schema.
- Select boundaries ONLY with the supplied editorial unit IDs. Never invent
  timestamps and never split an editorial unit.
- After the optional cold open, all spans must remain in strict source order.
  Order chapters by their earliest selected unit, never by priority or headline
  strength. The application will safely normalize a simple ordering mistake,
  but it will never delete material to do so.
- Include every genuinely strong distinct topic that fits, but quality beats
  topic count. Use {min_chapters}-{max_chapters} chapters, at most 2 spans per
  chapter and at most {max_segments} body spans in total.
{chapter_rule}
- Every body span must be {min_segment_seconds}-{max_segment_seconds} seconds.
  Use a second chronological span for a topic only when its useful material is
  separated; do not create one oversized passage.
- A chapter must start with the original question, setup, or a self-contained
  statement. It must end after a complete answer or thought.
- Units whose boundary is `best_pause` are the fast-speaker fallback. They are
  allowed only when the selected multi-unit passage is still semantically
  complete; prefer `sentence` or `strong_pause` boundaries.
- Preserve short connective material that is needed to understand names,
  pronouns, claims, or the next answer. Remove greetings, sponsors, repeated
  points, housekeeping, and unrelated tangents.
- Choose an honest assembled duration between {target_min_seconds} and
  {target_max_seconds} seconds based on how much strong material exists. Never
  pad to ten minutes. `recommended_duration_seconds` includes the cold open.
- If fewer than {target_min_seconds} strong coherent seconds exist, or fewer
  than {min_chapters} distinct chapters can be filled, return `viable:false`,
  `cold_open:null`, and `chapters:[]`.

{cold_open_rules}

COPY:
- All generated copy must use TRANSCRIPT_LANGUAGE ({language}).
- `video_title` is truthful, compelling, and at most 100 characters.
- `youtube_description` is 2-4 sentences without timestamps.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_DURATION_SECONDS: {video_duration}
PLANNING_BLOCKS_JSON (each transcript unit appears exactly once):
{blocks_json}

Return only an object shaped like:
{{
  "viable": true,
  "video_title": "<title>",
  "youtube_description": "<description>",
  "recommended_duration_seconds": <{target_min_seconds}-{target_max_seconds}>,
  "duration_reason": "<brief reason based on content richness>",
  "cold_open": {{
    "id": "cold_open",
    "start_unit_id": "u000001",
    "end_unit_id": "u000003",
    "title": "Cold Open",
    "priority": 100,
    "reason": "<why it hooks>",
    "replay_in_body": false
  }},
  "chapters": [{{
    "id": "chapter_01",
    "title": "<short chapter title>",
    "topic": "<topic>",
    "priority": 85,
    "reason": "<why this belongs>",
    "spans": [{{
      "id": "chapter_01_span_01",
      "start_unit_id": "u000010",
      "end_unit_id": "u000030"
    }}]
  }}]
}}
"""


def longform_plan_rules(payload):
    """Prompt rules that must agree with the deterministic quality gate.
    LONGFORM_MIN_CHAPTERS=1 and LONGFORM_COLD_OPEN=0 are honoured here so the
    prompt does not demand what the validator will then reject."""
    try:
        min_chapters = max(1, int(payload.get("min_chapters", 2) or 1))
    except (TypeError, ValueError):
        min_chapters = 2
    if min_chapters > 1:
        chapter_rule = (
            f"- {min_chapters} distinct chapters are MANDATORY. A single-chapter plan is a\n"
            "  one-topic compilation, not a best-of edit, and is rejected. If the source\n"
            "  truly carries one subject, split it into its distinct parts (for example\n"
            "  question/setup, development, conclusion) and give each its own chapter."
        )
    else:
        chapter_rule = (
            "- A single strong chapter is acceptable when the source truly carries one\n"
            "  subject. Do not invent artificial splits only to raise the chapter count."
        )
    if payload.get("cold_open_enabled", True):
        try:
            cold_open_max = max(5, int(float(payload.get("cold_open_max_seconds", 15) or 15)))
        except (TypeError, ValueError):
            cold_open_max = 15
        cold_open_rules = (
            "COLD OPEN:\n"
            f"- Prefer one self-contained 5-{cold_open_max} second highlight with a complete\n"
            "  beginning and ending. Never end on a comma, conjunction, or unfinished\n"
            "  question.\n"
            "- Set `replay_in_body:true` only if removing those same units from the later\n"
            "  chronological passage would damage its context. Otherwise choose a highlight\n"
            "  outside the body spans so it is not duplicated."
        )
    else:
        cold_open_rules = (
            "COLD OPEN:\n"
            "- Cold opens are disabled for this job. Return `cold_open:null` and start\n"
            "  the video directly with the first chapter."
        )
    return {"chapter_rule": chapter_rule, "cold_open_rules": cold_open_rules}


LONGFORM_REVIEW_PROMPT_TEMPLATE = """
You are the independent final-cut editor. Review the proposed assembled video,
not the source in isolation. Fix awkward openings, unfinished endings,
unexplained references, duplicate teaser content, and unnatural transitions.

REVIEW RULES:
- Return only valid JSON matching the requested schema.
- Keep the `id` of every surviving cold open/span unchanged. Each span may use
  `start_unit_id` only from its own `start_candidate_units` and `end_unit_id`
  only from its own `end_candidate_units` for that exact `segment_id` in
  BOUNDARY_NEIGHBORHOODS_JSON. Never connect IDs from two segments or edges.
- You may extend or contract a span to neighboring units, or drop a chapter
  that cannot connect naturally. Never reorder body chapters.
- Never drop a chapter merely to repair source order. Simple source-order
  mistakes are normalized deterministically before this review. If remaining
  chapter ranges interleave and cannot be fixed without changing selection,
  reject the plan instead of deleting a strong topic.
- If REPAIR_FEEDBACK contains `avoidable_chapter_drop:<id>`, keep that chapter
  from DRAFT_PLAN and repair its boundaries/transitions. The application has
  deliberately restored the last lossless plan for this retry.
- The final plan must keep at least {min_chapters} chapters. Dropping a chapter
  below that limit is not allowed; reject the plan instead. If REPAIR_FEEDBACK
  reports `too_few_chapters`, regroup the EXISTING spans into at least
  {min_chapters} chapters by giving each its own chapter object and title —
  keep every span `id` and its unit boundaries unchanged. If the spans are not
  thematically separable, return `approved:false`.
- Retain as many strong distinct topics as possible, but drop a topic instead
  of approving a confusing transition.
- The final assembled duration must remain {target_min_seconds}-
  {target_max_seconds} seconds, every body span must remain
  {min_segment_seconds}-{max_segment_seconds} seconds, and the plan may contain
  no more than {max_segments} body spans or {max_chapters} chapters.
- Every opening must provide its question/setup; every ending must finish the
  thought. A final video ending on words such as "dass", "und", or a comma is
  always incomplete.
- Audit EVERY segment separately. In each boundary neighborhood, compare
  `current_start_unit_id` and `current_end_unit_id` with the surrounding units
  in `start_candidate_units` and `end_candidate_units`. If material after the cut answers,
  explains, qualifies, or resolves the selected claim, set
  `continuation_needed:true` and do not approve that boundary. Punctuation by
  itself never proves that a thought is complete.
- A `best_pause` boundary can pass only when the surrounding language proves
  the thought is complete.
- Decide whether the cold open needs to replay later for comprehension. If
  `replay_in_body:false`, its source units must not overlap the body.
- Score the FINAL joins, after all revisions, from 0-100. Include exactly one
  join object for each adjacent pair in the final assembled plan. Set
  `context_complete:true` only when a viewer can follow the new passage without
  missing information.
- `approved:true` requires no critical issue, a complete ending, every join at
  least 80, every segment boundary complete, and an honest overall score of at
  least 85.

FINAL_VERIFICATION_ONLY: {final_verification}
When this is true, this is a fresh critic pass after a repair. Copy DRAFT_PLAN
exactly into `plan`: do not alter IDs, boundaries, order, chapters, replay
policy, title, or description. Do not drop anything. Only audit the repaired
cut and reject it when any opening, ending, continuation, or join is weak.

REPAIR_REQUIRED: {repair_required}
REPAIR_FEEDBACK_JSON:
{repair_feedback_json}

TRANSCRIPT_LANGUAGE: {language}
DRAFT_PLAN_JSON:
{draft_plan_json}

ASSEMBLED_AND_BOUNDARY_CONTEXT_JSON:
{review_context_json}

Return only:
{{
  "approved": true,
  "overall_score": 90,
  "ending_complete": true,
  "critical_issues": [],
  "dropped_chapter_ids": [],
  "boundary_reviews": [{{
    "segment_id": "span_id",
    "opening_complete": true,
    "ending_complete": true,
    "continuation_needed": false,
    "issue": ""
  }}],
  "joins": [{{
    "id": "left_span_id->right_span_id",
    "score": 90,
    "context_complete": true,
    "issue": ""
  }}],
  "plan": <the complete final LongformPlanV2 object>
}}
"""


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_json_candidate(text: str) -> str:
    cleaned = _strip_code_fences(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start:end + 1]
    return cleaned


def _escape_invalid_unicode_escapes(text: str) -> str:
    chars = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "u":
            hex_digits = text[i + 2:i + 6]
            if len(hex_digits) < 4 or any(ch not in "0123456789abcdefABCDEF" for ch in hex_digits):
                chars.append("\\\\u")
                i += 2
                continue
        chars.append(text[i])
        i += 1
    return "".join(chars)


def _require_json_object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Gemini response JSON root must be an object.")
    return value


def _validate_response_payload(mode: str, value: object) -> dict:
    payload = _require_json_object(value)
    schema = _RESPONSE_SCHEMAS[mode]
    if hasattr(schema, "model_validate"):
        validated = schema.model_validate(payload)
    else:  # Pydantic v1 compatibility for older local installations.
        validated = schema.parse_obj(payload)
    return validated.model_dump() if hasattr(validated, "model_dump") else validated.dict()


def _parse_json_response_text(text: str) -> dict:
    if not text:
        raise ValueError("Gemini returned an empty response body.")
    cleaned = _strip_code_fences(text).replace("\x00", "").strip()
    if not cleaned:
        raise ValueError("Gemini response did not contain a JSON object.")

    parse_attempts = [cleaned]
    sanitized_cleaned = _escape_invalid_unicode_escapes(cleaned)
    if sanitized_cleaned != cleaned:
        parse_attempts.append(sanitized_cleaned)
    last_error: Optional[Exception] = None
    for parse_candidate in parse_attempts:
        try:
            return _require_json_object(json.loads(parse_candidate))
        except json.JSONDecodeError as e:
            last_error = e

    # Gemini occasionally wraps an otherwise valid object in prose. Recover
    # that object only after the complete response failed JSON decoding. This
    # preserves the true root type for valid arrays such as ``[{...}]`` so they
    # cannot masquerade as an object by having their outer brackets trimmed.
    candidate = _extract_json_candidate(cleaned).strip()
    if candidate != cleaned:
        recovery_attempts = [candidate]
        sanitized_candidate = _escape_invalid_unicode_escapes(candidate)
        if sanitized_candidate != candidate:
            recovery_attempts.append(sanitized_candidate)
        for parse_candidate in recovery_attempts:
            try:
                return _require_json_object(json.loads(parse_candidate))
            except json.JSONDecodeError as e:
                last_error = e
    raise ValueError(f"Failed to parse Gemini JSON response: {last_error}")


def _get_response_text(response) -> str:
    try:
        text = response.text
        if text:
            return text
    except Exception:
        pass

    parts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(part_text)
    return "\n".join(parts).strip()


def _enum_text(value) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw)
    return text if text else None


def _response_diagnostics(response) -> dict:
    """Keep the reason for empty Gemini bodies instead of losing the batch."""
    prompt_feedback = getattr(response, "prompt_feedback", None)
    diagnostics = {
        "prompt_feedback": {
            "block_reason": _enum_text(getattr(prompt_feedback, "block_reason", None)),
            "block_reason_message": getattr(prompt_feedback, "block_reason_message", None),
        },
        "candidates": [],
    }
    for candidate in getattr(response, "candidates", []) or []:
        safety_ratings = []
        for rating in getattr(candidate, "safety_ratings", []) or []:
            safety_ratings.append({
                "category": _enum_text(getattr(rating, "category", None)),
                "probability": _enum_text(getattr(rating, "probability", None)),
                "blocked": bool(getattr(rating, "blocked", False)),
            })
        diagnostics["candidates"].append({
            "finish_reason": _enum_text(getattr(candidate, "finish_reason", None)),
            "finish_message": getattr(candidate, "finish_message", None),
            "safety_ratings": safety_ratings,
        })
    return diagnostics


def _write_worker_result(path: str, result: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def _calculate_cost_analysis(response, model_name: str) -> Optional[dict]:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    prices = lookup_model_prices(model_name)
    price_estimated = prices is None
    if prices is None:
        # Unknown model: conservative estimate so the UI shows something sane.
        prices = (0.50, 3.00)
    input_price_per_million, output_price_per_million = prices
    prompt_tokens = usage.prompt_token_count or 0
    output_tokens = usage.candidates_token_count or 0
    # Thinking tokens bill at the output rate even though they are invisible.
    thinking_tokens = getattr(usage, "thoughts_token_count", 0) or 0
    input_cost = (prompt_tokens / 1_000_000) * input_price_per_million
    output_cost = ((output_tokens + thinking_tokens) / 1_000_000) * output_price_per_million
    total_cost = input_cost + output_cost
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
        "model": model_name,
        "price_estimated": price_estimated,
    }


def _thinking_config_from_env(
    model_name: str,
    env_name: str = "GEMINI_THINKING_SCORE",
    default: str = "off",
):
    """Build a model-compatible thinking config from one environment value."""
    raw = (os.getenv(env_name) or default).strip().lower()
    if raw in ("", "off", "0", "none", "false"):
        return None
    try:
        if raw.isdigit():
            return genai_types.ThinkingConfig(thinking_budget=int(raw))
        if raw in ("low", "high"):
            if model_name.startswith("gemini-3"):
                return genai_types.ThinkingConfig(thinking_level=raw)
            return genai_types.ThinkingConfig(thinking_budget=2048 if raw == "low" else 8192)
    except Exception as e:
        _log(f"⚠️ Ignoring {env_name}={raw!r}: {e}")
    return None


def _config_for_strategy(strategy: str, mode: str, model_name: str) -> genai_types.GenerateContentConfig:
    # The detail stage writes creative copy (hooks/descriptions) — it gets a
    # high temperature; timestamps are validated and word-snapped afterwards.
    # The score stage stays precise. Fallback strategies get conservative.
    creative = mode == "detail"
    kwargs = {
        "response_mime_type": "application/json",
        "candidate_count": 1,
    }
    if mode == "longform_plan_v2":
        kwargs["temperature"] = {
            "structured-schema": 0.2,
            "strict-json": 0.1,
            "json-text-recovery": 0.0,
        }.get(strategy, 0.1)
    elif mode == "longform_review":
        kwargs["temperature"] = 0.0
    elif strategy == "strict-json":
        kwargs["temperature"] = 0.7 if creative else 0.1
    elif strategy == "json-text-recovery":
        kwargs["temperature"] = 0.2 if creative else 0.0
    else:  # structured-schema primary temperature
        kwargs["temperature"] = 0.9 if creative else 0.2

    if strategy == "structured-schema":
        kwargs["response_schema"] = _RESPONSE_SCHEMAS[mode]
        if mode == "score":
            thinking = _thinking_config_from_env(model_name)
            if thinking is not None:
                kwargs["thinking_config"] = thinking
        elif mode in {"longform_plan_v2", "longform_review"}:
            thinking = _thinking_config_from_env(
                model_name,
                env_name="GEMINI_THINKING_LONGFORM",
                default="low",
            )
            if thinking is not None:
                kwargs["thinking_config"] = thinking
    return genai_types.GenerateContentConfig(**kwargs)


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(description="Run one Gemini request for clip or long-form analysis.")
    parser.add_argument(
        "--mode",
        choices=["score", "detail", "longform_plan_v2", "longform_review"],
        required=True,
    )
    parser.add_argument("--input", dest="input_path", required=True)
    parser.add_argument("--output", dest="output_path", required=True)
    parser.add_argument("--strategy", default="structured-schema")
    parser.add_argument("--model", default="gemini-2.5-flash")
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("Missing GEMINI_API_KEY.")

    with open(args.input_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    model_name = args.model
    client = genai.Client(api_key=api_key)
    config = _config_for_strategy(args.strategy, args.mode, model_name)
    language = str(payload.get("language") or "unknown")

    template = {
        "score": SCORE_PROMPT_TEMPLATE,
        "detail": DETAIL_PROMPT_TEMPLATE,
        "longform_plan_v2": LONGFORM_PLAN_V2_PROMPT_TEMPLATE,
        "longform_review": LONGFORM_REVIEW_PROMPT_TEMPLATE,
    }[args.mode]
    prompt = template.format(
        video_duration=payload["video_duration"],
        language=language,
        windows_json=json.dumps(payload.get("windows", []), ensure_ascii=False),
        blocks_json=json.dumps(payload.get("blocks", []), ensure_ascii=False),
        draft_plan_json=json.dumps(payload.get("draft_plan", {}), ensure_ascii=False),
        review_context_json=json.dumps(payload.get("review_context", {}), ensure_ascii=False),
        repair_required=str(bool(payload.get("repair_required"))).lower(),
        final_verification=str(bool(payload.get("final_verification"))).lower(),
        repair_feedback_json=json.dumps(payload.get("repair_feedback", []), ensure_ascii=False),
        target_min_seconds=payload.get("target_min_seconds", 480),
        target_max_seconds=payload.get("target_max_seconds", 600),
        min_segment_seconds=payload.get("min_segment_seconds", 20),
        max_segment_seconds=payload.get("max_segment_seconds", 240),
        max_segments=payload.get("max_segments", 12),
        max_chapters=payload.get("max_chapters", 6),
        min_chapters=payload.get("min_chapters", 2),
        **longform_plan_rules(payload),
    )

    item_count = len(payload.get("windows") or payload.get("blocks") or [])
    _log(f"🤖 Gemini worker request: mode={args.mode} strategy={args.strategy} model={model_name} items={item_count}")
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config,
        )
    except Exception as exc:
        result = {
            "status": "error",
            "mode": args.mode,
            "error_type": "api_error",
            "error": str(exc),
            "payload": None,
            "cost_analysis": None,
            "raw_text": "",
            "diagnostics": {},
        }
        _write_worker_result(args.output_path, result)
        _log(f"❌ Gemini worker API error: {exc}")
        return 2

    raw_text = _get_response_text(response)
    diagnostics = _response_diagnostics(response)
    cost_analysis = _calculate_cost_analysis(response, model_name)
    # With response_schema the SDK returns an already-validated object; fall
    # back to the text-repair path only when that is unavailable.
    try:
        parsed_obj = getattr(response, "parsed", None)
        if parsed_obj is not None:
            parsed = parsed_obj.model_dump() if hasattr(parsed_obj, "model_dump") else parsed_obj
        else:
            parsed = _parse_json_response_text(raw_text)
        parsed = _validate_response_payload(args.mode, parsed)
    except Exception as exc:
        block_reason = diagnostics.get("prompt_feedback", {}).get("block_reason")
        explicitly_blocked = bool(block_reason and "UNSPECIFIED" not in block_reason.upper())
        error_type = "blocked_response" if explicitly_blocked else (
            "empty_response" if not raw_text else "invalid_response"
        )
        result = {
            "status": "error",
            "mode": args.mode,
            "error_type": error_type,
            "error": str(exc),
            "payload": None,
            "cost_analysis": cost_analysis,
            "raw_text": raw_text,
            "diagnostics": diagnostics,
        }
        _write_worker_result(args.output_path, result)
        _log(f"❌ Gemini worker response error ({error_type}): {exc}")
        return 3
    result = {
        "status": "success",
        "mode": args.mode,
        "payload": parsed,
        "cost_analysis": cost_analysis,
        "raw_text": raw_text,
        "diagnostics": diagnostics,
    }
    _write_worker_result(args.output_path, result)
    _log(f"✅ Gemini worker success: mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
