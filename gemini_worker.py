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


class LongformSegmentModel(BaseModel):
    start: float
    end: float
    chapter_title: str
    priority: int
    continuity_importance: int
    required: bool
    role: str
    reason: str


class LongformPlanResponse(BaseModel):
    viable: bool
    video_title: str
    youtube_description: str
    segments: List[LongformSegmentModel]


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
Select the MOST viral candidate windows from this batch.

Rules:
- Return only valid JSON.
- Choose up to 3 windows from this batch.
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


LONGFORM_PLAN_PROMPT_TEMPLATE = """
You are a senior YouTube editor and story producer. Build ONE coherent long-form
video that feels deliberately edited, never like unrelated shorts stitched
together. The requested assembled duration is {target_min_seconds} to
{target_max_seconds} seconds.

STORY RULES:
- Return only valid JSON.
- Preserve one clear through-line: context/setup -> development/bridges -> payoff.
- Apart from the optional cold open, every segment MUST be in chronological
  source order and use absolute seconds from the source video.
- Prefer a few substantial passages (60-180 seconds is ideal). Every body
  segment must be between {min_segment_seconds} and {max_segment_seconds} seconds.
- Preserve connective tissue that a viewer needs to understand the next scene.
  Remove greetings, sponsors, housekeeping, repetition and unrelated tangents.
- Begin on a sentence boundary and end after a complete sentence.
- Scores indicate audience interest, but narrative coherence beats a higher score.
- Use `role` values setup, bridge, body or payoff. Mark indispensable context
  with `required: true`, and rate `continuity_importance` from 0 to 100.
- If the material cannot honestly sustain {target_min_seconds} coherent seconds,
  return `viable: false` instead of padding it. In that case still return all
  schema fields, using an empty `youtube_description` and `segments: []`.

COLD OPEN:
- You may add exactly one first segment with `role: "cold_open"`: a 5-15 second
  teaser of the strongest moment, without spoiling the complete payoff.
- Its content may appear again later in full chronological context.

CHAPTERS AND COPY:
- Give related consecutive body segments the same short `chapter_title`, written
  in TRANSCRIPT_LANGUAGE. Use a distinct title when the story actually advances.
- `video_title` must be curiosity-driven, truthful and at most 100 characters.
- `youtube_description` must be 2-4 sentences without timestamps; chapters are
  appended by the application.
- All generated text must use TRANSCRIPT_LANGUAGE ({language}).

TRANSCRIPT_LANGUAGE: {language}
VIDEO_DURATION_SECONDS: {video_duration}
WINDOWS_JSON:
{windows_json}

Return only:
{{
  "viable": true,
  "video_title": "<title>",
  "youtube_description": "<2-4 sentences>",
  "segments": [
    {{
      "start": <absolute seconds>,
      "end": <absolute seconds>,
      "chapter_title": "<short chapter>",
      "priority": <integer 0-100>,
      "continuity_importance": <integer 0-100>,
      "required": <true or false>,
      "role": "cold_open|setup|bridge|body|payoff",
      "reason": "<short editorial reason>"
    }}
  ]
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


def _parse_json_response_text(text: str) -> dict:
    if not text:
        raise ValueError("Gemini returned an empty response body.")
    candidate = _extract_json_candidate(text).replace("\x00", "").strip()
    if not candidate:
        raise ValueError("Gemini response did not contain a JSON object.")
    parse_attempts = [candidate]
    sanitized_candidate = _escape_invalid_unicode_escapes(candidate)
    if sanitized_candidate != candidate:
        parse_attempts.append(sanitized_candidate)
    last_error: Optional[Exception] = None
    for parse_candidate in parse_attempts:
        try:
            return json.loads(parse_candidate)
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


def _thinking_config_from_env(model_name: str):
    """GEMINI_THINKING_SCORE: off (default) | low | high | <token budget>.

    Applied only to the scoring stage. Gemini 3 models take thinking_level,
    Gemini 2.5 takes thinking_budget; returns None (= model default) if the
    setting is off or the SDK rejects the config."""
    raw = (os.getenv("GEMINI_THINKING_SCORE") or "off").strip().lower()
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
        _log(f"⚠️ Ignoring GEMINI_THINKING_SCORE={raw!r}: {e}")
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
    if mode == "longform_plan":
        kwargs["temperature"] = {
            "structured-schema": 0.4,
            "strict-json": 0.2,
            "json-text-recovery": 0.1,
        }.get(strategy, 0.2)
    elif strategy == "strict-json":
        kwargs["temperature"] = 0.7 if creative else 0.1
    elif strategy == "json-text-recovery":
        kwargs["temperature"] = 0.2 if creative else 0.0
    else:  # structured-schema primary temperature
        kwargs["temperature"] = 0.9 if creative else 0.2

    if strategy == "structured-schema":
        kwargs["response_schema"] = {
            "detail": DetailResponse,
            "score": ScoreResponse,
            "longform_plan": LongformPlanResponse,
        }[mode]
        if mode == "score":
            thinking = _thinking_config_from_env(model_name)
            if thinking is not None:
                kwargs["thinking_config"] = thinking
    return genai_types.GenerateContentConfig(**kwargs)


def main() -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(description="Run one Gemini request for clip or long-form analysis.")
    parser.add_argument("--mode", choices=["score", "detail", "longform_plan"], required=True)
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
        "longform_plan": LONGFORM_PLAN_PROMPT_TEMPLATE,
    }[args.mode]
    prompt = template.format(
        video_duration=payload["video_duration"],
        language=language,
        windows_json=json.dumps(payload["windows"], ensure_ascii=False),
        target_min_seconds=payload.get("target_min_seconds", 480),
        target_max_seconds=payload.get("target_max_seconds", 600),
        min_segment_seconds=payload.get("min_segment_seconds", 20),
        max_segment_seconds=payload.get("max_segment_seconds", 240),
    )

    _log(f"🤖 Gemini worker request: mode={args.mode} strategy={args.strategy} model={model_name} items={len(payload.get('windows', []))}")
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
