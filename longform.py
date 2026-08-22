"""Validation and FFmpeg planning helpers for coherent long-form outputs.

This module intentionally has no third-party dependencies.  It turns an
untrusted model response into a deterministic, chronological edit decision;
the actual subprocess execution stays in :mod:`main`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from typing import Iterable, Optional

from clip_selection import snap_clip_to_words


TARGET_ASPECT_RATIO = 16 / 9
SENTENCE_END_RE = re.compile(r"[.!?\u2026][\"'\u2019\u201d)\]]*$")
ALLOWED_ROLES = {"cold_open", "setup", "bridge", "body", "payoff"}
PLANNER_VERSION = 2
QUALITY_GATE_VERSION = 2
DEFAULT_STRONG_PAUSE_SECONDS = 0.55
DEFAULT_FALLBACK_WINDOW_SECONDS = 24.0
DEFAULT_BOUNDARY_PADDING_SECONDS = 0.20
DEFAULT_REVIEW_BOUNDARY_WINDOW_SECONDS = 75.0
DEFAULT_REVIEW_BOUNDARY_MAX_UNITS = 24
OBVIOUS_TRAILING_CONNECTORS = {
    "aber", "als", "dass", "damit", "denn", "oder", "und", "weil", "wenn", "wie",
    "and", "as", "because", "but", "if", "or", "that", "when", "while",
}
OBVIOUS_LEADING_CONNECTORS = {
    "damit", "dass", "obwohl", "sodass", "während", "weil", "wenn",
    "because", "if", "unless", "when", "while",
}


def _word_start(word: dict) -> float:
    return float(word.get("s", word.get("start", 0.0)))


def _word_end(word: dict) -> float:
    return float(word.get("e", word.get("end", _word_start(word))))


def _flatten_transcript_words(transcript_result: dict) -> list[dict]:
    words = []
    for segment in transcript_result.get("segments", []) or []:
        segment_words = segment.get("words") or []
        for word in segment_words:
            try:
                start = float(word.get("s", word.get("start")))
                end = float(word.get("e", word.get("end")))
            except (TypeError, ValueError):
                continue
            text = str(word.get("w", word.get("word", "")) or "")
            if not text.strip() or end <= start:
                continue
            words.append({"w": text, "s": start, "e": end})

    # Word timestamps are normally present.  Segment-level fallback keeps the
    # V2 planner usable for imported transcripts while marking every boundary
    # as a relaxed fallback rather than pretending it is a sentence boundary.
    if not words:
        for segment in transcript_result.get("segments", []) or []:
            text = str(segment.get("text") or "").strip()
            try:
                start = float(segment.get("start", 0.0))
                end = float(segment.get("end", start))
            except (TypeError, ValueError):
                continue
            if text and end > start:
                words.append({"w": text, "s": start, "e": end, "segment_fallback": True})
    return sorted(words, key=lambda item: (_word_start(item), _word_end(item)))


def _joined_word_text(words: list[dict]) -> str:
    tokens = [str(word.get("w") or "") for word in words]
    # faster-whisper normally includes leading spaces in word tokens. Imported
    # transcripts do not always do that, so avoid turning "Hello" + "world"
    # into "Helloworld" while preserving Whisper's punctuation spacing.
    if any(token[:1].isspace() for token in tokens[1:]):
        return "".join(tokens).strip()
    return " ".join(token.strip() for token in tokens if token.strip()).strip()


def build_editorial_units(
    transcript_result: dict,
    video_duration: Optional[float] = None,
    *,
    strong_pause_seconds: float = DEFAULT_STRONG_PAUSE_SECONDS,
    fallback_window_seconds: float = DEFAULT_FALLBACK_WINDOW_SECONDS,
    min_unit_seconds: float = 1.0,
    boundary_padding_seconds: float = DEFAULT_BOUNDARY_PADDING_SECONDS,
) -> list[dict]:
    """Build stable, selectable transcript units for Longform V2.

    A unit normally closes on terminal punctuation or a strong word gap.  Fast
    speakers are handled by selecting the best local pause once a bounded
    window is reached; this prevents a rigid pause threshold from producing
    minute-long uncuttable regions.  Relaxed boundaries stay explicitly marked
    so Gemini's independent review can reject semantically incomplete cuts.
    """
    words = _flatten_transcript_words(transcript_result)
    if not words:
        return []

    strong_pause = max(0.0, float(strong_pause_seconds))
    fallback_window = max(2.0, float(fallback_window_seconds))
    minimum = max(0.0, float(min_unit_seconds))
    padding = max(0.0, float(boundary_padding_seconds))
    source_end = max(
        float(video_duration or 0.0),
        max(_word_end(word) for word in words),
    )

    units = []
    cursor = 0
    previous_boundary = "source_start"
    while cursor < len(words):
        first_start = _word_start(words[cursor])
        scan = cursor
        best_pause_index = None
        best_pause_gap = -1.0
        boundary_index = None
        boundary_type = None
        boundary_gap = 0.0

        while scan < len(words):
            current = words[scan]
            current_end = _word_end(current)
            next_start = _word_start(words[scan + 1]) if scan + 1 < len(words) else source_end
            gap = max(0.0, next_start - current_end)
            elapsed = current_end - first_start
            if elapsed >= minimum and gap > best_pause_gap:
                best_pause_index = scan
                best_pause_gap = gap

            terminal = bool(SENTENCE_END_RE.search(_word_text(current)))
            segment_fallback = bool(current.get("segment_fallback"))
            if terminal:
                boundary_index = scan
                boundary_type = "sentence"
                boundary_gap = gap
                break
            if gap >= strong_pause:
                boundary_index = scan
                boundary_type = "strong_pause"
                boundary_gap = gap
                break
            if segment_fallback:
                boundary_index = scan
                boundary_type = "best_pause"
                boundary_gap = gap
                break
            if elapsed >= fallback_window:
                boundary_index = best_pause_index if best_pause_index is not None else scan
                boundary_type = "best_pause"
                chosen_end = _word_end(words[boundary_index])
                chosen_next = (
                    _word_start(words[boundary_index + 1])
                    if boundary_index + 1 < len(words) else source_end
                )
                boundary_gap = max(0.0, chosen_next - chosen_end)
                break
            if scan == len(words) - 1:
                boundary_index = scan
                boundary_type = "source_end"
                boundary_gap = gap
                break
            scan += 1

        if boundary_index is None:
            boundary_index = len(words) - 1
            boundary_type = "source_end"

        unit_words = words[cursor:boundary_index + 1]
        first_word_start = _word_start(unit_words[0])
        last_word_end = _word_end(unit_words[-1])
        raw_previous_word_end = (
            _word_end(words[cursor - 1]) if cursor > 0 else max(0.0, first_word_start)
        )
        raw_next_word_start = (
            _word_start(words[boundary_index + 1])
            if boundary_index + 1 < len(words) else source_end
        )
        # Word timestamps can overlap by a few milliseconds. Empty safe
        # windows are preferable to cutting into the first/last selected word.
        previous_word_end = min(first_word_start, raw_previous_word_end)
        next_word_start = max(last_word_end, raw_next_word_start)
        start_gap = max(0.0, first_word_start - previous_word_end)
        end_gap = max(0.0, next_word_start - last_word_end)
        cut_start = max(previous_word_end, first_word_start - min(padding, start_gap / 2.0))
        cut_end = min(source_end, last_word_end + min(padding, end_gap / 2.0))
        unit_id = f"u{len(units) + 1:06d}"
        units.append({
            "id": unit_id,
            "start": round(first_word_start, 3),
            "end": round(last_word_end, 3),
            "cut_start": round(cut_start, 3),
            "cut_end": round(cut_end, 3),
            "start_cut_window": [round(previous_word_end, 3), round(first_word_start, 3)],
            "end_cut_window": [round(last_word_end, 3), round(next_word_start, 3)],
            "text": _joined_word_text(unit_words),
            "start_boundary": previous_boundary,
            "end_boundary": boundary_type,
            "end_pause_seconds": round(boundary_gap, 3),
            "boundary_confidence": (
                "high" if boundary_type in {"sentence", "strong_pause", "source_end"} else "relaxed"
            ),
        })
        previous_boundary = boundary_type
        cursor = boundary_index + 1
    return units


def transcript_fingerprint(units: Iterable[dict]) -> str:
    compact = [
        [
            str(unit.get("id") or ""),
            round(float(unit.get("start", 0.0)), 2),
            round(float(unit.get("end", 0.0)), 2),
            round(float(unit.get("cut_start", unit.get("start", 0.0))), 2),
            round(float(unit.get("cut_end", unit.get("end", 0.0))), 2),
            str(unit.get("start_boundary") or ""),
            str(unit.get("end_boundary") or ""),
            str(unit.get("text") or ""),
        ]
        for unit in units
    ]
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_planning_blocks(
    units: Iterable[dict],
    scored_windows: Optional[Iterable[dict]] = None,
    *,
    block_seconds: float = 120.0,
) -> list[dict]:
    """Pack every transcript unit exactly once and project Shorts scores onto it."""
    units = list(units)
    scored = list(scored_windows or [])

    def projected_score(start: float, end: float) -> tuple[int, str]:
        weighted = 0.0
        weight = 0.0
        strongest = None
        for item in scored:
            try:
                item_start = float(item.get("start", 0.0))
                item_end = float(item.get("end", 0.0))
                item_score = int(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            overlap = max(0.0, min(end, item_end) - max(start, item_start))
            if overlap <= 0:
                continue
            weighted += overlap * item_score
            weight += overlap
            if strongest is None or item_score > strongest[0]:
                strongest = (item_score, str(item.get("reason") or ""))
        score = int(round(weighted / weight)) if weight else 0
        return score, strongest[1] if strongest else ""

    blocks = []
    current = []
    target = max(20.0, float(block_seconds))
    for unit in units:
        if current and float(unit["end"]) - float(current[0]["start"]) > target:
            start = float(current[0]["start"])
            end = float(current[-1]["end"])
            score, reason = projected_score(start, end)
            blocks.append(_planning_block(len(blocks) + 1, current, score, reason))
            current = []
        current.append(unit)
    if current:
        start = float(current[0]["start"])
        end = float(current[-1]["end"])
        score, reason = projected_score(start, end)
        blocks.append(_planning_block(len(blocks) + 1, current, score, reason))
    return blocks


def _planning_block(index: int, units: list[dict], score: int, reason: str) -> dict:
    return {
        "id": f"block_{index:03d}",
        "start": round(float(units[0]["start"]), 2),
        "end": round(float(units[-1]["end"]), 2),
        "interest_score": int(score),
        "score_reason": str(reason or ""),
        "units": [
            {
                "id": unit["id"],
                "s": round(float(unit["start"]), 2),
                "e": round(float(unit["end"]), 2),
                "text": unit["text"],
                "boundary": unit["end_boundary"],
            }
            for unit in units
        ],
    }


def _span_from_units(
    raw_span: dict,
    units: list[dict],
    lookup: dict[str, int],
    *,
    segment_id: str,
    chapter_id: str,
    chapter_title: str,
    priority: int,
    reason: str,
) -> tuple[dict, int, int]:
    start_id = str(raw_span.get("start_unit_id") or "")
    end_id = str(raw_span.get("end_unit_id") or "")
    if start_id not in lookup or end_id not in lookup:
        raise ValueError(f"Unknown editorial unit in span {segment_id}: {start_id!r}..{end_id!r}")
    start_index = lookup[start_id]
    end_index = lookup[end_id]
    if end_index < start_index:
        raise ValueError(f"Reversed editorial unit range in span {segment_id}.")
    first = units[start_index]
    last = units[end_index]
    segment = {
        "segment_id": segment_id,
        "chapter_id": chapter_id,
        "chapter_title": chapter_title,
        "priority": int(_clamp(float(priority), 0, 100)),
        "reason": str(reason or ""),
        "unit_start_id": start_id,
        "unit_end_id": end_id,
        "start": round(float(first["cut_start"]), 3),
        "end": round(float(last["cut_end"]), 3),
        "start_cut_window": list(first.get("start_cut_window") or [first["start"], first["start"]]),
        "end_cut_window": list(last.get("end_cut_window") or [last["end"], last["end"]]),
        "start_boundary": str(first.get("start_boundary") or "unknown"),
        "end_boundary": str(last.get("end_boundary") or "unknown"),
        "boundary_confidence": (
            "relaxed" if (
                str(first.get("start_boundary")) == "best_pause"
                or str(last.get("end_boundary")) == "best_pause"
            ) else "high"
        ),
        "first_unit_text": str(first.get("text") or "").strip(),
        "last_unit_text": str(last.get("text") or "").strip(),
        "transcript_text": " ".join(str(unit.get("text") or "") for unit in units[start_index:end_index + 1]).strip(),
    }
    return segment, start_index, end_index


def order_unit_plan_chronologically(payload: dict, units: Iterable[dict]) -> tuple[dict, set[str]]:
    """Sort a model plan by source unit without deleting any selected material.

    Gemini sometimes returns otherwise useful chapters in priority order instead
    of source order.  A deterministic sort is safer than asking a repair pass to
    discard one of those chapters.  Interleaving chapter ranges remain visible
    to :func:`resolve_unit_plan` and still fail its chronology invariant.
    """
    if not isinstance(payload, dict):
        raise ValueError("Longform V2 plan was not a JSON object.")
    ordered = copy.deepcopy(payload)
    units = list(units)
    lookup = {str(unit.get("id")): index for index, unit in enumerate(units)}
    chapters = ordered.get("chapters")
    if not isinstance(chapters, list):
        return ordered, set()

    original_chapter_positions = {
        str(chapter.get("id") or f"chapter_{index + 1:02d}"): index
        for index, chapter in enumerate(chapters)
        if isinstance(chapter, dict)
    }
    span_order_changed = set()

    def span_position(span: object, fallback: int) -> tuple[int, int]:
        if not isinstance(span, dict):
            return len(units) + fallback, fallback
        return lookup.get(str(span.get("start_unit_id") or ""), len(units) + fallback), fallback

    annotated = []
    for chapter_index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            annotated.append((len(units) + chapter_index, chapter_index, chapter))
            continue
        chapter_id = str(chapter.get("id") or f"chapter_{chapter_index + 1:02d}")
        spans = chapter.get("spans")
        if isinstance(spans, list):
            original_span_ids = [str(span.get("id") or "") for span in spans if isinstance(span, dict)]
            sorted_spans = [
                span for _position, _fallback, span in sorted(
                    (span_position(span, span_index) + (span,) for span_index, span in enumerate(spans)),
                    key=lambda item: (item[0], item[1]),
                )
            ]
            chapter["spans"] = sorted_spans
            sorted_span_ids = [str(span.get("id") or "") for span in sorted_spans if isinstance(span, dict)]
            if sorted_span_ids != original_span_ids:
                span_order_changed.add(chapter_id)
        valid_starts = [
            lookup[str(span.get("start_unit_id"))]
            for span in (chapter.get("spans") or [])
            if isinstance(span, dict) and str(span.get("start_unit_id")) in lookup
        ]
        first_start = min(valid_starts) if valid_starts else len(units) + chapter_index
        annotated.append((first_start, chapter_index, chapter))

    ordered_chapters = [item[2] for item in sorted(annotated, key=lambda item: (item[0], item[1]))]
    ordered["chapters"] = ordered_chapters
    moved_chapters = set(span_order_changed)
    for new_index, chapter in enumerate(ordered_chapters):
        if not isinstance(chapter, dict):
            continue
        chapter_id = str(chapter.get("id") or f"chapter_{new_index + 1:02d}")
        if original_chapter_positions.get(chapter_id) != new_index:
            moved_chapters.add(chapter_id)
    return ordered, moved_chapters


def plan_selection_signature(payload: dict) -> tuple:
    """Return the immutable editorial selection used by verification-only review."""
    if not isinstance(payload, dict):
        return ()
    cold = payload.get("cold_open")
    cold_signature = None
    if isinstance(cold, dict):
        cold_signature = (
            str(cold.get("id") or "cold_open"),
            str(cold.get("start_unit_id") or ""),
            str(cold.get("end_unit_id") or ""),
            bool(cold.get("replay_in_body", False)),
        )
    chapters = []
    for chapter in payload.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        spans = tuple(
            (
                str(span.get("id") or ""),
                str(span.get("start_unit_id") or ""),
                str(span.get("end_unit_id") or ""),
            )
            for span in chapter.get("spans") or []
            if isinstance(span, dict)
        )
        chapters.append((str(chapter.get("id") or ""), spans))
    return cold_signature, tuple(chapters)


def resolve_unit_plan(
    payload: dict,
    units: Iterable[dict],
    *,
    min_output_seconds: float = 240.0,
    max_output_seconds: float = 600.0,
    min_segment_seconds: float = 20.0,
    max_segment_seconds: float = 240.0,
    max_chapters: int = 6,
    min_chapters: int = 2,
    max_segments: int = 12,
    cold_open_enabled: bool = True,
    cold_open_max_seconds: float = 15.0,
    allowed_unit_ids: Optional[set[str]] = None,
    allowed_unit_ids_by_segment: Optional[dict[str, object]] = None,
) -> dict:
    """Resolve a Gemini V2 plan from stable IDs to deterministic timestamps."""
    if not isinstance(payload, dict):
        raise ValueError("Longform V2 plan was not a JSON object.")
    title = str(payload.get("video_title") or "Long Video").strip()[:100]
    description = str(payload.get("youtube_description") or "").strip()
    if payload.get("viable") is False:
        return {
            "planner_version": PLANNER_VERSION,
            "viable": False,
            "video_title": title,
            "youtube_description": description,
            "segments": [],
            "total_duration": 0.0,
            "target_min_seconds": float(min_output_seconds),
            "target_max_seconds": float(max_output_seconds),
            "validation_issues": ["model_marked_not_viable"],
        }

    units = list(units)
    if not units:
        raise ValueError("Longform V2 has no editorial units.")
    lookup = {str(unit.get("id")): index for index, unit in enumerate(units)}
    chapters = payload.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("Longform V2 plan did not contain chapters.")

    issues = []
    body_segments = []
    body_ranges = []
    chapter_ids = set()
    segment_ids = set()
    for chapter_index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            raise ValueError("Longform V2 chapter was not an object.")
        chapter_id = str(chapter.get("id") or f"chapter_{chapter_index:02d}")
        if chapter_id in chapter_ids:
            issues.append(f"duplicate_chapter_id:{chapter_id}")
        chapter_ids.add(chapter_id)
        chapter_title = str(chapter.get("title") or f"Teil {chapter_index}").strip()[:80]
        spans = chapter.get("spans")
        if not isinstance(spans, list) or not spans:
            issues.append(f"empty_chapter:{chapter_id}")
            continue
        if len(spans) > 2:
            issues.append(f"too_many_spans_in_chapter:{chapter_id}")
        for span_index, raw_span in enumerate(spans, start=1):
            segment_id = str(raw_span.get("id") or f"{chapter_id}_span_{span_index}")
            if segment_id in segment_ids:
                issues.append(f"duplicate_segment_id:{segment_id}")
            segment_ids.add(segment_id)
            if allowed_unit_ids_by_segment is not None:
                segment_allowed_ids = allowed_unit_ids_by_segment.get(segment_id)
                if not segment_allowed_ids:
                    issues.append(f"unknown_review_segment:{segment_id}")
                else:
                    if isinstance(segment_allowed_ids, dict):
                        allowed_starts = set(segment_allowed_ids.get("start") or [])
                        allowed_ends = set(segment_allowed_ids.get("end") or [])
                    else:
                        allowed_starts = allowed_ends = set(segment_allowed_ids)
                    if (
                        str(raw_span.get("start_unit_id") or "") not in allowed_starts
                        or str(raw_span.get("end_unit_id") or "") not in allowed_ends
                    ):
                        issues.append(f"unit_outside_own_review_context:{segment_id}")
            elif allowed_unit_ids is not None and (
                str(raw_span.get("start_unit_id") or "") not in allowed_unit_ids
                or str(raw_span.get("end_unit_id") or "") not in allowed_unit_ids
            ):
                issues.append(f"unit_outside_review_context:{segment_id}")
            segment, start_index, end_index = _span_from_units(
                raw_span,
                units,
                lookup,
                segment_id=segment_id,
                chapter_id=chapter_id,
                chapter_title=chapter_title,
                priority=int(chapter.get("priority", 50) or 50),
                reason=str(chapter.get("reason") or ""),
            )
            segment_duration = _duration(segment)
            if segment_duration < float(min_segment_seconds) - 0.001:
                issues.append(f"short_segment:{segment_id}")
            if segment_duration > float(max_segment_seconds) + 0.001:
                issues.append(f"long_segment:{segment_id}")
            opening_text = str(segment.get("first_unit_text") or "").strip()
            ending_text = str(segment.get("last_unit_text") or "").rstrip()
            opening_tokens = re.findall(r"[\w\u00c0-\u024f]+", opening_text)
            ending_tokens = re.findall(r"[\w\u00c0-\u024f]+", ending_text)
            if (
                opening_tokens
                and opening_tokens[0].casefold() in OBVIOUS_LEADING_CONNECTORS
                and "," not in opening_text
                and ";" not in opening_text
            ):
                issues.append(f"obviously_incomplete_start:{segment_id}")
            if ending_text.endswith((",", ";", ":")):
                issues.append(f"obviously_incomplete_end:{segment_id}")
            elif ending_tokens and ending_tokens[-1].casefold() in OBVIOUS_TRAILING_CONNECTORS:
                issues.append(f"obviously_incomplete_end:{segment_id}")
            if body_ranges and start_index <= body_ranges[-1][1]:
                issues.append(f"non_chronological_or_overlapping:{segment_id}")
            body_ranges.append((start_index, end_index, segment_id))
            body_segments.append(segment)

    if len(chapters) > int(max_chapters):
        issues.append("chapter_cap_exceeded")
    # A single chapter is a compilation of one topic, not the planned best-of.
    # Count only chapters that actually carry a span: an empty chapter must not
    # satisfy the minimum. The review pass may regroup existing spans, so this
    # stays repairable instead of being an instant rejection.
    effective_chapters = {segment.get("chapter_id") for segment in body_segments}
    if body_segments and len(effective_chapters) < max(1, int(min_chapters)):
        issues.append("too_few_chapters")
    if len(body_segments) > int(max_segments):
        issues.append("segment_cap_exceeded")
    if body_segments:
        body_segments[0]["role"] = "setup"
        for segment in body_segments[1:-1]:
            segment["role"] = "body"
        body_segments[-1]["role"] = "payoff"

    teaser = None
    cold_payload = payload.get("cold_open")
    if cold_open_enabled and isinstance(cold_payload, dict):
        cold_segment_id = str(cold_payload.get("id") or "cold_open")
        if cold_segment_id in segment_ids:
            issues.append(f"duplicate_segment_id:{cold_segment_id}")
        if allowed_unit_ids_by_segment is not None:
            cold_allowed_ids = allowed_unit_ids_by_segment.get(cold_segment_id)
            if not cold_allowed_ids:
                issues.append(f"unknown_review_segment:{cold_segment_id}")
            else:
                if isinstance(cold_allowed_ids, dict):
                    allowed_starts = set(cold_allowed_ids.get("start") or [])
                    allowed_ends = set(cold_allowed_ids.get("end") or [])
                else:
                    allowed_starts = allowed_ends = set(cold_allowed_ids)
                if (
                    str(cold_payload.get("start_unit_id") or "") not in allowed_starts
                    or str(cold_payload.get("end_unit_id") or "") not in allowed_ends
                ):
                    issues.append(f"unit_outside_own_review_context:{cold_segment_id}")
        elif allowed_unit_ids is not None and (
            str(cold_payload.get("start_unit_id") or "") not in allowed_unit_ids
            or str(cold_payload.get("end_unit_id") or "") not in allowed_unit_ids
        ):
            issues.append(f"unit_outside_review_context:{cold_segment_id}")
        teaser, cold_start_index, cold_end_index = _span_from_units(
            cold_payload,
            units,
            lookup,
            segment_id=cold_segment_id,
            chapter_id="cold_open",
            chapter_title=str(cold_payload.get("title") or "Cold Open")[:80],
            priority=int(cold_payload.get("priority", 100) or 100),
            reason=str(cold_payload.get("reason") or ""),
        )
        teaser["role"] = "cold_open"
        teaser["replay_in_body"] = bool(cold_payload.get("replay_in_body", False))
        teaser_duration = _duration(teaser)
        if teaser_duration < 5.0 - 0.001 or teaser_duration > float(cold_open_max_seconds) + 0.001:
            issues.append("invalid_cold_open_duration")
        teaser_opening = str(teaser.get("first_unit_text") or "").strip()
        teaser_ending = str(teaser.get("last_unit_text") or "").rstrip()
        teaser_opening_tokens = re.findall(r"[\w\u00c0-\u024f]+", teaser_opening)
        teaser_ending_tokens = re.findall(r"[\w\u00c0-\u024f]+", teaser_ending)
        if (
            teaser_opening_tokens
            and teaser_opening_tokens[0].casefold() in OBVIOUS_LEADING_CONNECTORS
            and "," not in teaser_opening
            and ";" not in teaser_opening
        ):
            issues.append("obviously_incomplete_start:cold_open")
        if teaser_ending.endswith((",", ";", ":")) or (
            teaser_ending_tokens and teaser_ending_tokens[-1].casefold() in OBVIOUS_TRAILING_CONNECTORS
        ):
            issues.append("obviously_incomplete_end:cold_open")
        overlaps_body = any(
            max(cold_start_index, start_index) <= min(cold_end_index, end_index)
            for start_index, end_index, _segment_id in body_ranges
        )
        if overlaps_body and not teaser["replay_in_body"]:
            issues.append("cold_open_overlap_without_replay")
    elif cold_payload:
        issues.append("cold_open_disabled")

    segments = ([teaser] if teaser else []) + body_segments
    total = round(sum(_duration(segment) for segment in segments), 3)
    if total < float(min_output_seconds) - 0.5:
        issues.append("result_below_adaptive_minimum")
    if total > float(max_output_seconds) + 0.5:
        issues.append("result_exceeds_hard_maximum")
    if not body_segments:
        issues.append("no_body_segments")

    return {
        "planner_version": PLANNER_VERSION,
        "viable": bool(body_segments),
        "video_title": title,
        "youtube_description": description,
        "recommended_duration_seconds": int(payload.get("recommended_duration_seconds", round(total)) or round(total)),
        "duration_reason": str(payload.get("duration_reason") or "").strip(),
        "segments": segments,
        "total_duration": total,
        "target_min_seconds": float(min_output_seconds),
        "target_max_seconds": float(max_output_seconds),
        "validation_issues": list(dict.fromkeys(issues)),
        "warnings": [],
    }


def build_review_context(
    plan: dict,
    units: Iterable[dict],
    *,
    neighbor_units: int = 4,
    neighbor_seconds: float = DEFAULT_REVIEW_BOUNDARY_WINDOW_SECONDS,
    max_neighbor_units: int = DEFAULT_REVIEW_BOUNDARY_MAX_UNITS,
) -> dict:
    """Build local but semantically useful context around every planned cut.

    The old fixed four-unit window could be only a few seconds for a fast
    speaker, which prevented the reviewer from reaching the question before a
    cut or the conclusion after it.  The window is now time based with a hard
    unit cap so it remains local and predictable even for a long source.
    """
    units = list(units)
    lookup = {str(unit.get("id")): index for index, unit in enumerate(units)}
    assembled = []
    neighborhoods = []
    for segment in plan.get("segments") or []:
        start_index = lookup.get(str(segment.get("unit_start_id")))
        end_index = lookup.get(str(segment.get("unit_end_id")))
        if start_index is None or end_index is None:
            continue
        assembled.append({
            "segment_id": segment.get("segment_id"),
            "chapter_id": segment.get("chapter_id"),
            "chapter_title": segment.get("chapter_title"),
            "role": segment.get("role"),
            "source_start": segment.get("start"),
            "source_end": segment.get("end"),
            "start_boundary": segment.get("start_boundary"),
            "end_boundary": segment.get("end_boundary"),
            "text": segment.get("transcript_text"),
        })
        unit_radius = max(int(neighbor_units), 0)
        unit_cap = max(1, int(max_neighbor_units))
        time_radius = max(float(neighbor_seconds), 0.0)

        def candidate_indices(center_index: int, *, edge: str) -> list[int]:
            center = units[center_index]
            anchor = float(center["start"] if edge == "start" else center["end"])

            def eligible(candidate_index: int) -> bool:
                candidate = units[candidate_index]
                point = float(candidate["start"] if edge == "start" else candidate["end"])
                return (
                    abs(candidate_index - center_index) <= unit_radius
                    or abs(point - anchor) <= time_radius + 0.001
                )

            search_radius = max(0, unit_cap - 1)
            earlier = [
                index
                for index in range(center_index - 1, max(-1, center_index - search_radius - 1), -1)
                if eligible(index)
            ]
            later = [
                index
                for index in range(center_index + 1, min(len(units), center_index + search_radius + 1))
                if eligible(index)
            ]
            # A start boundary primarily needs earlier setup; an end boundary
            # primarily needs later resolution. Keep a few candidates on the
            # opposite side so Gemini can still contract an over-wide span.
            primary, secondary = (earlier, later) if edge == "start" else (later, earlier)
            secondary_quota = min(unit_radius, len(secondary), unit_cap - 1)
            primary_quota = min(len(primary), unit_cap - 1 - secondary_quota)
            selected = [center_index] + primary[:primary_quota] + secondary[:secondary_quota]
            if len(selected) < unit_cap:
                leftovers = primary[primary_quota:] + secondary[secondary_quota:]
                selected.extend(leftovers[:unit_cap - len(selected)])
            return sorted(selected)

        start_indices = candidate_indices(start_index, edge="start")
        end_indices = candidate_indices(end_index, edge="end")

        def boundary_candidates(items):
            return [
                {
                    "id": unit["id"],
                    "s": round(float(unit["start"]), 2),
                    "e": round(float(unit["end"]), 2),
                    "text": unit["text"],
                    "start_boundary": unit["start_boundary"],
                    "end_boundary": unit["end_boundary"],
                    "boundary_confidence": unit["boundary_confidence"],
                }
                for unit in items
            ]

        neighborhoods.append({
            "segment_id": segment.get("segment_id"),
            "current_start_unit_id": segment.get("unit_start_id"),
            "current_end_unit_id": segment.get("unit_end_id"),
            "start_candidate_units": boundary_candidates([units[index] for index in start_indices]),
            "end_candidate_units": boundary_candidates([units[index] for index in end_indices]),
        })
    joins = [
        {
            "id": f"{left.get('segment_id')}->{right.get('segment_id')}",
            "left_segment_id": left.get("segment_id"),
            "right_segment_id": right.get("segment_id"),
            "source_gap_seconds": round(float(right.get("source_start", 0)) - float(left.get("source_end", 0)), 3),
        }
        for left, right in zip(assembled, assembled[1:])
    ]
    return {
        "assembled_segments": assembled,
        "boundary_neighborhoods": neighborhoods,
        "expected_joins": joins,
    }


def assess_editorial_quality(
    plan: dict,
    review_payload: dict,
    *,
    minimum_overall_score: int = 85,
    minimum_join_score: int = 80,
    protected_chapter_ids: Optional[set[str]] = None,
) -> list[str]:
    """Combine deterministic invariants with Gemini's independent review."""
    issues = list(plan.get("validation_issues") or [])
    if not isinstance(review_payload, dict):
        return list(dict.fromkeys(issues + ["missing_editorial_review"]))
    if not bool(review_payload.get("approved")):
        issues.append("review_not_approved")
    try:
        overall = int(review_payload.get("overall_score", 0) or 0)
    except (TypeError, ValueError):
        overall = 0
    if overall < int(minimum_overall_score):
        issues.append("overall_review_score_below_threshold")
    if not bool(review_payload.get("ending_complete")):
        issues.append("incomplete_ending")
    if any(str(item).strip() for item in (review_payload.get("critical_issues") or [])):
        issues.append("critical_review_issue")

    joins_by_id = {
        str(item.get("id")): item
        for item in (review_payload.get("joins") or [])
        if isinstance(item, dict)
    }
    segments = list(plan.get("segments") or [])
    boundary_reviews_by_id = {
        str(item.get("segment_id")): item
        for item in (review_payload.get("boundary_reviews") or [])
        if isinstance(item, dict)
    }
    for segment in segments:
        segment_id = str(segment.get("segment_id") or "")
        boundary_review = boundary_reviews_by_id.get(segment_id)
        if not boundary_review:
            issues.append(f"missing_boundary_review:{segment_id}")
            continue
        if not bool(boundary_review.get("opening_complete")):
            issues.append(f"incomplete_opening:{segment_id}")
        if (
            not bool(boundary_review.get("ending_complete"))
            or bool(boundary_review.get("continuation_needed"))
        ):
            issues.append(f"incomplete_segment_ending:{segment_id}")

    for left, right in zip(segments, segments[1:]):
        join_id = f"{left.get('segment_id')}->{right.get('segment_id')}"
        join = joins_by_id.get(join_id)
        if not join:
            issues.append(f"missing_join_review:{join_id}")
            continue
        try:
            score = int(join.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        if score < int(minimum_join_score) or not bool(join.get("context_complete")):
            issues.append(f"weak_join:{join_id}")

    retained_chapter_ids = {
        str(segment.get("chapter_id") or "")
        for segment in segments
        if segment.get("role") != "cold_open"
    }
    for chapter_id in sorted(set(protected_chapter_ids or set())):
        if chapter_id and chapter_id not in retained_chapter_ids:
            issues.append(f"avoidable_chapter_drop:{chapter_id}")

    return list(dict.fromkeys(issues))


def _duration(segment: dict) -> float:
    return max(0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)))


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _word_text(word: dict) -> str:
    return str(word.get("w") or word.get("word") or "").strip()


def build_chapters(segments: Iterable[dict]) -> list[dict]:
    """Build valid chapter offsets on the assembled timeline.

    Chapter markers closer than ten seconds are folded into their predecessor;
    this keeps a 5–15 second cold open from invalidating YouTube's chapter list.
    """
    timeline = []
    offset = 0.0
    previous_title = None
    for index, segment in enumerate(segments):
        title = str(segment.get("chapter_title") or ("Intro" if index == 0 else f"Teil {index + 1}")).strip()
        if index == 0 or title != previous_title:
            timeline.append({"time_seconds": round(offset, 3), "title": title})
        offset += _duration(segment)
        previous_title = title

    if not timeline:
        return []
    timeline[0]["time_seconds"] = 0.0
    filtered = [timeline[0]]
    for item in timeline[1:]:
        if item["time_seconds"] - filtered[-1]["time_seconds"] >= 10.0:
            filtered.append(item)
    while len(filtered) > 1 and offset - filtered[-1]["time_seconds"] < 10.0:
        filtered.pop()
    return [
        {**item, "formatted": format_timestamp(item["time_seconds"])}
        for item in filtered
    ]


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_youtube_description(description: str, chapters: Iterable[dict]) -> str:
    chapter_lines = [
        f"{item.get('formatted') or format_timestamp(item.get('time_seconds', 0))} {item.get('title') or 'Kapitel'}"
        for item in chapters
    ]
    base = str(description or "").strip()
    if not chapter_lines:
        return base
    # Timestamp lines are self-describing and recognized directly by YouTube;
    # omitting a hard-coded heading keeps generated copy language-neutral.
    return "\n\n".join(part for part in (base, "\n".join(chapter_lines)) if part)


def needs_16_9_canvas(width: int, height: int, tolerance: float = 0.0) -> bool:
    if not width or not height:
        return True
    # yuv420p/H.264 requires even dimensions.  Route rare odd-sized 16:9
    # rasters through the configured even canvas instead of failing the cut.
    if int(width) % 2 or int(height) % 2:
        return True
    if tolerance:
        return not math.isclose(float(width) / float(height), TARGET_ASPECT_RATIO, rel_tol=tolerance)
    # Cross multiplication avoids float tolerance accidentally accepting a
    # near-16:9 source whose encoded canvas is still not actually 16:9.
    return (int(width) * 9) != (int(height) * 16)


def blurred_16_9_filter(width: int = 1920, height: int = 1080) -> str:
    width = max(2, int(width) // 2 * 2)
    height = max(2, int(height) // 2 * 2)
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=28[bg_blur];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg_fit];"
        f"[bg_blur][fg_fit]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[video]"
    )


def segment_cut_command(
    input_video,
    start,
    end,
    output_path,
    fade_seconds=0.01,
    *,
    filter_complex: Optional[str] = None,
) -> list[str]:
    start_value = max(0.0, float(start))
    duration = max(0.001, float(end) - start_value)
    fade = min(max(0.0, float(fade_seconds)), duration / 4.0)
    fade_out_start = max(0.0, duration - fade)
    command = [
        "ffmpeg", "-y", "-ss", f"{start_value:.3f}", "-i", os.fspath(input_video),
        "-t", f"{duration:.3f}",
    ]
    if filter_complex:
        command.extend(["-filter_complex", filter_complex, "-map", "[video]", "-map", "0:a?"])
    else:
        # An exact 16:9 pixel raster can still carry a non-square sample aspect
        # ratio.  Reset SAR so the delivered display canvas is genuinely 16:9.
        command.extend(["-map", "0:v:0", "-map", "0:a?", "-vf", "setsar=1"])
    command.extend([
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
    ])
    if fade > 0:
        command.extend([
            "-af", f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}",
        ])
    command.extend(["-movflags", "+faststart", os.fspath(output_path)])
    return command


def concat_manifest_text(filenames: Iterable[str]) -> str:
    lines = []
    for filename in filenames:
        normalized = os.path.abspath(os.fspath(filename)).replace("\\", "/")
        escaped = normalized.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + ("\n" if lines else "")


def concat_command(manifest_path, output_path) -> list[str]:
    return [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", os.fspath(manifest_path),
        "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", os.fspath(output_path),
    ]
