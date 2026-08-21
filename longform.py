"""Validation and FFmpeg planning helpers for coherent long-form outputs.

This module intentionally has no third-party dependencies.  It turns an
untrusted model response into a deterministic, chronological edit decision;
the actual subprocess execution stays in :mod:`main`.
"""

from __future__ import annotations

import math
import os
import re
from typing import Iterable, Optional

from clip_selection import snap_clip_to_words


TARGET_ASPECT_RATIO = 16 / 9
SENTENCE_END_RE = re.compile(r"[.!?\u2026][\"'\u2019\u201d)\]]*$")
PROTECTED_ROLES = {"setup", "bridge", "payoff"}
ALLOWED_ROLES = {"cold_open", "setup", "bridge", "body", "payoff"}


def _duration(segment: dict) -> float:
    return max(0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)))


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _word_text(word: dict) -> str:
    return str(word.get("w") or word.get("word") or "").strip()


def _copy_segment(segment: dict) -> dict:
    copied = dict(segment)
    copied["start"] = round(float(copied["start"]), 3)
    copied["end"] = round(float(copied["end"]), 3)
    return copied


def merge_plan_segments(segments: Iterable[dict], merge_gap_seconds: float) -> list[dict]:
    """Merge overlapping/nearby body segments while never absorbing a teaser."""
    cold_opens = [_copy_segment(item) for item in segments if item.get("role") == "cold_open"]
    body = sorted(
        (_copy_segment(item) for item in segments if item.get("role") != "cold_open"),
        key=lambda item: (item["start"], item["end"]),
    )
    merged: list[dict] = []
    for segment in body:
        if not merged or segment["start"] > merged[-1]["end"] + merge_gap_seconds:
            merged.append(segment)
            continue

        previous = merged[-1]
        previous["end"] = round(max(previous["end"], segment["end"]), 3)
        previous["priority"] = max(int(previous.get("priority", 50)), int(segment.get("priority", 50)))
        previous["continuity_importance"] = max(
            int(previous.get("continuity_importance", 50)),
            int(segment.get("continuity_importance", 50)),
        )
        previous["required"] = bool(previous.get("required") or segment.get("required"))
        if previous.get("role") == "body" and segment.get("role") in PROTECTED_ROLES:
            previous["role"] = segment["role"]

    return cold_opens[:1] + merged


def _is_protected(segment: dict, index: int, body_count: int) -> bool:
    return bool(
        segment.get("required")
        or segment.get("role") in PROTECTED_ROLES
        or index == 0
        or index == body_count - 1
    )


def _sentence_end_at_or_before(words: Optional[list[dict]], start: float, desired_end: float) -> Optional[float]:
    if not words:
        return None
    candidates = []
    fallback = []
    for word in words:
        try:
            word_start = float(word.get("s", word.get("start", 0.0)))
            word_end = float(word.get("e", word.get("end", word_start)))
        except (TypeError, ValueError):
            continue
        if word_start < start or word_end > desired_end:
            continue
        fallback.append(word_end)
        if SENTENCE_END_RE.search(_word_text(word)):
            candidates.append(word_end)
    if candidates:
        return max(candidates)
    return max(fallback) if fallback else None


def _trim_segment_end(
    segment: dict,
    new_duration: float,
    *,
    words: Optional[list[dict]],
    min_segment_seconds: float,
) -> Optional[dict]:
    desired_end = float(segment["start"]) + max(min_segment_seconds, new_duration)
    snapped_end = _sentence_end_at_or_before(words, float(segment["start"]) + min_segment_seconds, desired_end)
    end = snapped_end if snapped_end is not None else desired_end
    if end - float(segment["start"]) < min_segment_seconds:
        return None
    result = _copy_segment(segment)
    result["end"] = round(min(float(segment["end"]), end), 3)
    return result


def _split_long_segment(
    segment: dict,
    max_segment_seconds: float,
    *,
    min_segment_seconds: float,
    words: Optional[list[dict]],
) -> list[dict]:
    segment_duration = _duration(segment)
    if segment_duration <= max_segment_seconds + 0.001:
        return [segment]

    piece_count = int(math.ceil(segment_duration / max_segment_seconds))
    if segment_duration + 0.001 < piece_count * min_segment_seconds:
        raise ValueError(
            "Segment cannot satisfy both the configured minimum and maximum durations."
        )

    pieces = []
    cursor = float(segment["start"])
    final_end = float(segment["end"])
    pieces_remaining = piece_count
    while pieces_remaining > 1:
        remaining_duration = final_end - cursor
        later_piece_count = pieces_remaining - 1
        min_piece_duration = max(
            min_segment_seconds,
            remaining_duration - later_piece_count * max_segment_seconds,
        )
        max_piece_duration = min(
            max_segment_seconds,
            remaining_duration - later_piece_count * min_segment_seconds,
        )
        if max_piece_duration + 0.001 < min_piece_duration:
            raise ValueError(
                "Segment cannot satisfy both the configured minimum and maximum durations."
            )

        desired_end = cursor + max_piece_duration
        split_end = _sentence_end_at_or_before(
            words,
            cursor + min_piece_duration,
            desired_end,
        )
        if split_end is None:
            split_end = desired_end
        split_end = _clamp(
            split_end,
            cursor + min_piece_duration,
            desired_end,
        )
        piece = _copy_segment(segment)
        piece["start"] = round(cursor, 3)
        piece["end"] = round(split_end, 3)
        pieces.append(piece)
        cursor = split_end
        pieces_remaining -= 1

    tail = _copy_segment(segment)
    tail["start"] = round(cursor, 3)
    tail["end"] = round(final_end, 3)
    pieces.append(tail)
    return pieces


def fit_plan_to_target(
    segments: Iterable[dict],
    target_min_seconds: float,
    target_max_seconds: float,
    *,
    words: Optional[list[dict]] = None,
    min_segment_seconds: float = 20.0,
) -> tuple[list[dict], list[str]]:
    """Fit an edit without deleting narrative connective tissue blindly.

    Explicitly required segments and setup/bridge/payoff roles are protected.
    Optional low-value segments may be removed only when the result remains at
    least the requested minimum.  Any remaining excess is shortened at a real
    sentence/word boundary where timestamps permit it.
    """
    result = [_copy_segment(item) for item in segments]
    warnings: list[str] = []
    total = sum(_duration(item) for item in result)
    if total <= target_max_seconds:
        if total < target_min_seconds:
            warnings.append("short_result")
        return result, warnings

    cold_open = [item for item in result if item.get("role") == "cold_open"][:1]
    body = [item for item in result if item.get("role") != "cold_open"]
    body_count = len(body)
    removable = [
        (index, item)
        for index, item in enumerate(body)
        if not _is_protected(item, index, body_count)
    ]
    removable.sort(
        key=lambda pair: (
            int(pair[1].get("priority", 50)) + int(pair[1].get("continuity_importance", 50)),
            _duration(pair[1]),
        )
    )

    removed_ids = set()
    for index, segment in removable:
        if total <= target_max_seconds:
            break
        segment_duration = _duration(segment)
        if total - segment_duration < target_min_seconds:
            continue
        removed_ids.add(index)
        total -= segment_duration
        warnings.append(f"dropped_optional_segment:{segment.get('chapter_title', index + 1)}")

    body = [item for index, item in enumerate(body) if index not in removed_ids]
    result = cold_open + body

    if total > target_max_seconds:
        excess = total - target_max_seconds
        # Prefer shortening low-continuity body material. Required segments are
        # a last resort, but a malformed model response still must not silently
        # produce a 20-minute video.
        candidates = sorted(
            enumerate(body),
            key=lambda pair: (
                bool(pair[1].get("required")),
                pair[1].get("role") in PROTECTED_ROLES,
                int(pair[1].get("continuity_importance", 50)),
                int(pair[1].get("priority", 50)),
                -_duration(pair[1]),
            ),
        )
        for index, segment in candidates:
            if excess <= 0.001:
                break
            available = _duration(segment) - min_segment_seconds
            if available <= 0:
                continue
            requested_cut = min(available, excess)
            trimmed = _trim_segment_end(
                segment,
                _duration(segment) - requested_cut,
                words=words,
                min_segment_seconds=min_segment_seconds,
            )
            if not trimmed:
                continue
            actual_cut = _duration(segment) - _duration(trimmed)
            if actual_cut <= 0:
                continue
            body[index] = trimmed
            excess -= actual_cut
            total -= actual_cut
            warnings.append(f"trimmed_at_sentence:{segment.get('chapter_title', index + 1)}")
        result = cold_open + body

    if total > target_max_seconds + 0.5:
        warnings.append("over_target_required_story")
    if total < target_min_seconds:
        warnings.append("short_result")
    return result, warnings


def _cap_body_segments(body: list[dict], max_count: int) -> tuple[list[dict], bool]:
    if len(body) <= max_count:
        return body, False
    protected = {
        index for index, item in enumerate(body)
        if _is_protected(item, index, len(body))
    }
    remaining_slots = max(0, max_count - len(protected))
    optional = [
        (index, item) for index, item in enumerate(body) if index not in protected
    ]
    optional.sort(
        key=lambda pair: (
            int(pair[1].get("priority", 50)) + int(pair[1].get("continuity_importance", 50)),
            _duration(pair[1]),
        ),
        reverse=True,
    )
    keep = protected | {index for index, _item in optional[:remaining_slots]}
    # If the model marked more segments required than the hard cap, retain the
    # strongest protected items but always preserve chronological endpoints.
    if len(keep) > max_count:
        endpoints = {0, len(body) - 1}
        ranked = sorted(
            (index for index in keep if index not in endpoints),
            key=lambda index: (
                int(body[index].get("continuity_importance", 50)),
                int(body[index].get("priority", 50)),
            ),
            reverse=True,
        )
        keep = endpoints | set(ranked[: max(0, max_count - len(endpoints))])
    return [item for index, item in enumerate(body) if index in keep], True


def normalize_longform_plan(
    payload,
    video_duration,
    words=None,
    *,
    min_segment_seconds=20.0,
    max_segment_seconds=240.0,
    merge_gap_seconds=4.0,
    target_min_seconds=480.0,
    target_max_seconds=600.0,
    max_segments=30,
    cold_open=True,
    cold_open_max_seconds=20.0,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Long-form plan was not a JSON object.")

    title = str(payload.get("video_title") or "Long Video").strip()[:100]
    description = str(payload.get("youtube_description") or "").strip()
    if payload.get("viable") is False:
        return {
            "viable": False,
            "video_title": title,
            "youtube_description": description,
            "segments": [],
            "total_duration": 0.0,
            "warnings": ["model_marked_not_viable"],
        }

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("Long-form plan did not contain a valid 'segments' array.")

    duration = max(0.0, float(video_duration))
    normalized = []
    collapsed = 0
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            collapsed += 1
            continue
        try:
            raw_start = float(raw.get("start"))
            raw_end = float(raw.get("end"))
        except (TypeError, ValueError):
            collapsed += 1
            continue
        start = _clamp(raw_start, 0.0, duration)
        end = _clamp(raw_end, 0.0, duration)
        if end <= start:
            collapsed += 1
            continue
        role = str(raw.get("role") or "body").strip().lower()
        if role not in ALLOWED_ROLES:
            role = "body"
        try:
            priority = int(_clamp(float(raw.get("priority", 50)), 0, 100))
        except (TypeError, ValueError):
            priority = 50
        try:
            continuity = int(_clamp(float(raw.get("continuity_importance", 50)), 0, 100))
        except (TypeError, ValueError):
            continuity = 50
        normalized.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "chapter_title": str(raw.get("chapter_title") or f"Teil {index + 1}").strip()[:80],
            "priority": priority,
            "continuity_importance": continuity,
            "required": bool(raw.get("required", False)),
            "role": role,
            "reason": str(raw.get("reason") or "").strip(),
        })

    if raw_segments and collapsed / len(raw_segments) > 0.30:
        raise ValueError("Long-form plan used invalid or likely window-relative timestamps.")
    if not normalized:
        raise ValueError("Long-form plan did not contain any usable segments.")

    warnings: list[str] = []
    cold_candidates = [item for item in normalized if item["role"] == "cold_open"]
    teaser = None
    if cold_open and cold_candidates:
        teaser = max(cold_candidates, key=lambda item: (item["priority"], item["continuity_importance"]))
        teaser = _copy_segment(teaser)
        teaser["end"] = min(teaser["end"], teaser["start"] + float(cold_open_max_seconds))
        teaser["start"], teaser["end"] = snap_clip_to_words(
            teaser["start"], teaser["end"], words or [], duration,
            min_duration=5.0, max_duration=float(cold_open_max_seconds), search_window=2.0,
        )
        if _duration(teaser) < 5.0:
            teaser["end"] = round(min(duration, teaser["start"] + 5.0), 3)
        teaser["chapter_title"] = teaser.get("chapter_title") or "Intro"
        teaser["required"] = True
    elif cold_candidates:
        warnings.append("cold_open_disabled")

    body = [item for item in normalized if item["role"] != "cold_open"]
    snapped_body = []
    for item in sorted(body, key=lambda segment: (segment["start"], segment["end"])):
        raw_pieces = _split_long_segment(
            item,
            float(max_segment_seconds),
            min_segment_seconds=float(min_segment_seconds),
            words=words or [],
        )
        if len(raw_pieces) > 1:
            warnings.append(f"split_long_segment:{item['chapter_title']}")
        for piece in raw_pieces:
            snapped = _copy_segment(piece)
            snapped["start"], snapped["end"] = snap_clip_to_words(
                piece["start"], piece["end"], words or [], duration,
                min_duration=float(min_segment_seconds),
                max_duration=float(max_segment_seconds),
                search_window=2.0,
            )
            if _duration(snapped) < min_segment_seconds:
                warnings.append(f"dropped_short_segment:{snapped['chapter_title']}")
                continue
            snapped_body.append(snapped)

    merged = merge_plan_segments(snapped_body, float(merge_gap_seconds))
    body = []
    for item in merged:
        if item.get("role") == "cold_open":
            continue
        pieces = _split_long_segment(
            item,
            float(max_segment_seconds),
            min_segment_seconds=float(min_segment_seconds),
            words=words or [],
        )
        if len(pieces) > 1:
            warnings.append(f"split_long_segment:{item['chapter_title']}")
        body.extend(pieces)
    body, capped = _cap_body_segments(body, max(1, int(max_segments) - (1 if teaser else 0)))
    if capped:
        warnings.append("segment_cap_applied")

    fitted, fit_warnings = fit_plan_to_target(
        ([teaser] if teaser else []) + body,
        float(target_min_seconds),
        float(target_max_seconds),
        words=words or [],
        min_segment_seconds=float(min_segment_seconds),
    )
    warnings.extend(fit_warnings)
    total = round(sum(_duration(item) for item in fitted), 3)
    # The target range is already scaled for explicitly requested short
    # sources.  Accepting half of that range would turn an advertised 8-10
    # minute edit into a four-minute success instead of invoking the bounded
    # score fallback (or reporting that no coherent edit is possible).
    viable = bool(
        fitted
        and total >= float(target_min_seconds) - 0.5
        and total <= float(target_max_seconds) + 0.5
    )
    if total > float(target_max_seconds) + 0.5:
        warnings.append("result_exceeds_hard_max")

    raw_chapter_count = 0
    previous_chapter_title = None
    for index, item in enumerate(fitted):
        chapter_title = str(item.get("chapter_title") or ("Intro" if index == 0 else f"Teil {index + 1}")).strip()
        if index == 0 or chapter_title != previous_chapter_title:
            raw_chapter_count += 1
        previous_chapter_title = chapter_title
    chapters = build_chapters(fitted)
    if len(chapters) < raw_chapter_count:
        warnings.append("short_chapter_markers_folded")
    if len(chapters) < 3:
        warnings.append("fewer_than_three_chapters")

    return {
        "viable": viable,
        "video_title": title,
        "youtube_description": description,
        "segments": fitted,
        "total_duration": total,
        "warnings": list(dict.fromkeys(warnings)),
    }


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
    fade_seconds=0.04,
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
