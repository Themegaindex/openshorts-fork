"""
Scene layout planning for the vertical renderer.

Per detected scene the renderer picks a layout: TRACK (zoom on one person),
SPLIT (two people cropped individually and stacked — the Opus-Clip podcast
look) or GENERAL (blurred-background wide layout). These helpers hold the
decision logic, the split-crop geometry and the smoothing that stops the
layout from flickering between camera cuts. Stdlib-only so everything stays
unit-testable without OpenCV.
"""

from dataclasses import dataclass
from typing import Optional

MIN_SCENE_SECONDS = 1.5
LOW_LAYOUT_CONFIDENCE = 0.55
DUO_STABLE_RATIO = 0.60
MULTI_PERSON_AMBIGUITY_RATIO = 0.40
GROUP_STABLE_RATIO = 0.40

# SPLIT requirements: two faces in most samples, horizontally separated by at
# least this fraction of the frame width (otherwise the two crops would show
# nearly the same image twice).
SPLIT_MIN_SEPARATION_FRACTION = 0.15
# Face sits slightly above panel center for natural headroom.
SPLIT_FACE_ANCHOR_Y = 0.42


@dataclass(frozen=True)
class SceneLayoutDecision:
    """Evidence-backed layout choice for one detected source shot."""

    strategy: str
    split_centers: Optional[list]
    confidence: float
    reason: str
    face_counts: tuple = ()
    person_counts: tuple = ()


def smooth_scene_strategies(
    strategies,
    scene_durations,
    scene_confidences=None,
    min_scene_seconds=MIN_SCENE_SECONDS,
    low_confidence=LOW_LAYOUT_CONFIDENCE,
):
    """Conservatively stabilize uncertain, short GENERAL islands.

    Source-shot boundaries are legitimate edit points, so a short but
    confident close-up must survive.  Only a short, low-confidence island
    surrounded by GENERAL may be widened to GENERAL.  The function never
    invents TRACK/SPLIT crops and never overwrites a long or confident shot.
    """
    if not strategies:
        return []
    result = list(strategies)
    durations = list(scene_durations) + [min_scene_seconds] * (len(result) - len(scene_durations))
    confidences = list(scene_confidences or [])
    confidences += [1.0] * (len(result) - len(confidences))

    for i in range(1, len(result) - 1):
        is_short = durations[i] < min_scene_seconds
        is_uncertain = confidences[i] < low_confidence
        surrounded_by_general = result[i - 1] == result[i + 1] == "GENERAL"
        if is_short and is_uncertain and surrounded_by_general:
            result[i] = "GENERAL"

    return result


def _two_largest_centers(boxes):
    """Centers of the two largest face boxes, sorted left-to-right."""
    by_area = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)[:2]
    centers = sorted(((b[0] + b[2] / 2.0, b[1] + b[3] / 2.0) for b in by_area), key=lambda c: c[0])
    return centers


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def person_head_center(box):
    """Approximate head position inside a full-person YOLO box (x, y, w, h):
    horizontally centered, vertically ~20% from the top of the body."""
    x, y, w, h = box
    return (x + w / 2.0, y + h * 0.2)


def _median_pair_centers(pair_samples):
    """Median left/right center over samples that each contain two centers."""
    lefts = [pair[0] for pair in pair_samples]
    rights = [pair[1] for pair in pair_samples]
    left_center = (_median([c[0] for c in lefts]), _median([c[1] for c in lefts]))
    right_center = (_median([c[0] for c in rights]), _median([c[1] for c in rights]))
    return left_center, right_center


def _ratio_matching(samples, predicate):
    return (sum(1 for sample in samples if predicate(sample)) / len(samples)) if samples else 0.0


def _separated_face_pairs(samples, frame_width):
    pairs = []
    min_separation = frame_width * SPLIT_MIN_SEPARATION_FRACTION
    for sample in samples:
        if len(sample) < 2:
            continue
        pair = _two_largest_centers(sample)
        if pair[1][0] - pair[0][0] >= min_separation:
            pairs.append(pair)
    return pairs


def _separated_person_pairs(samples, frame_width):
    pairs = []
    min_separation = frame_width * SPLIT_MIN_SEPARATION_FRACTION
    for sample in samples:
        if len(sample) < 2:
            continue
        largest = sorted(sample, key=lambda b: b[2] * b[3], reverse=True)[:2]
        pair = sorted((person_head_center(box) for box in largest), key=lambda center: center[0])
        if pair[1][0] - pair[0][0] >= min_separation:
            pairs.append(pair)
    return pairs


def decide_scene_layout_detailed(face_samples, frame_width, layout_style="smart", person_samples=None):
    """Pick the layout for one scene from sampled detections.

    face_samples: one list of (x, y, w, h) face boxes per sampled frame.
    person_samples: optional matching lists of full-person YOLO boxes — the
    robust signal on wide shots, where the short-range face model misses
    distant or profile faces entirely.
    layout_style: "smart" (split two-person shots), "zoom" (legacy
    TRACK/GENERAL behavior) or "wide" (always the blurred wide layout).

    Returns a SceneLayoutDecision with confidence and evidence.  Smart mode is
    deliberately conservative: ambiguous multi-person evidence goes GENERAL,
    never TRACK, so detector uncertainty cannot start speaker ping-pong.
    """
    samples = [s for s in face_samples if s is not None]
    counts = [len(s) for s in samples]
    avg = (sum(counts) / len(counts)) if counts else 0.0
    p_samples = [s for s in (person_samples or []) if s is not None]
    person_counts = [len(s) for s in p_samples]

    def decision(strategy, centers, confidence, reason):
        return SceneLayoutDecision(
            strategy=strategy,
            split_centers=centers,
            confidence=max(0.0, min(1.0, float(confidence))),
            reason=reason,
            face_counts=tuple(counts),
            person_counts=tuple(person_counts),
        )

    if layout_style == "wide":
        return decision("GENERAL", None, 1.0, "wide layout requested")
    if layout_style == "zoom":
        # Legacy behavior: groups and empty shots go wide, single goes zoom.
        if avg > 1.2 or avg < 0.5:
            return decision("GENERAL", None, 0.75, "zoom mode group/unknown")
        return decision("TRACK", None, min(1.0, _ratio_matching(samples, lambda s: len(s) == 1)), "zoom mode single face")

    # --- smart ---
    face_group_ratio = _ratio_matching(samples, lambda sample: len(sample) >= 3)
    person_group_ratio = _ratio_matching(p_samples, lambda sample: len(sample) >= 3)
    group_ratio = max(face_group_ratio, person_group_ratio)
    if group_ratio >= GROUP_STABLE_RATIO or avg > 2.5:
        return decision("GENERAL", None, max(group_ratio, min(1.0, avg / 3.0)), "three or more people")

    face_pairs = _separated_face_pairs(samples, frame_width)
    person_pairs = _separated_person_pairs(p_samples, frame_width)
    face_pair_ratio = (len(face_pairs) / len(samples)) if samples else 0.0
    person_pair_ratio = (len(person_pairs) / len(p_samples)) if p_samples else 0.0
    duo_ratio = max(face_pair_ratio, person_pair_ratio)

    if duo_ratio >= DUO_STABLE_RATIO:
        # Faces give the best head anchor when they are themselves stable;
        # otherwise use profile-safe whole-person detections.
        pairs = face_pairs if face_pair_ratio >= DUO_STABLE_RATIO else person_pairs
        centers = _median_pair_centers(pairs)
        return decision("SPLIT", [centers[0], centers[1]], duo_ratio, "two separated people persist")

    face_multi_ratio = _ratio_matching(samples, lambda sample: len(sample) >= 2)
    person_multi_ratio = _ratio_matching(p_samples, lambda sample: len(sample) >= 2)
    multi_ratio = max(face_multi_ratio, person_multi_ratio)
    if multi_ratio >= MULTI_PERSON_AMBIGUITY_RATIO:
        return decision("GENERAL", None, multi_ratio, "multi-person evidence is not stable enough to split")

    face_single_ratio = _ratio_matching(samples, lambda sample: len(sample) == 1)
    person_single_ratio = _ratio_matching(p_samples, lambda sample: len(sample) == 1)
    single_ratio = max(face_single_ratio, person_single_ratio)
    if single_ratio >= DUO_STABLE_RATIO:
        return decision("TRACK", None, single_ratio, "one person persists")

    return decision("GENERAL", None, max(0.5, 1.0 - max(single_ratio, multi_ratio)), "people count is uncertain")


def decide_scene_layout(face_samples, frame_width, layout_style="smart", person_samples=None):
    """Backward-compatible tuple API used by existing callers/tests."""
    detailed = decide_scene_layout_detailed(
        face_samples,
        frame_width,
        layout_style=layout_style,
        person_samples=person_samples,
    )
    return detailed.strategy, detailed.split_centers


def sample_scene_frames(start_frame, end_frame, fps, min_samples=5, max_samples=24, samples_per_second=1.0):
    """Evenly sample a shot with duration-aware coverage and bounded cost."""
    span = max(0, int(end_frame) - int(start_frame))
    if span <= 0:
        return [int(start_frame)]
    duration = span / max(float(fps or 0), 1.0)
    sample_count = max(min_samples, int(round(duration * samples_per_second)))
    sample_count = min(max_samples, sample_count, span)
    return sorted({
        min(int(end_frame) - 1, int(start_frame + span * ((index + 0.5) / sample_count)))
        for index in range(sample_count)
    })


def split_crop_windows(src_w, src_h, out_w, out_h, centers, stacked=True):
    """Crop windows (one per person) for the split layout.

    stacked=True stacks the two panels vertically (9:16); False puts them
    side by side (1:1). Each window is capped at half the source width so the
    two crops never show largely the same image. Returns two integer boxes
    [(x1, y1, x2, y2), ...] in the same order as centers (left-to-right).
    """
    if stacked:
        panel_w, panel_h = out_w, max(1, out_h // 2)
    else:
        panel_w, panel_h = max(1, out_w // 2), out_h
    aspect = panel_w / panel_h

    win_w = min(src_w // 2, int(src_h * aspect))
    win_w = max(2, win_w)
    win_h = max(2, int(win_w / aspect))
    if win_h > src_h:
        win_h = src_h
        win_w = max(2, int(win_h * aspect))

    windows = []
    for cx, cy in centers:
        x1 = int(round(cx - win_w / 2.0))
        y1 = int(round(cy - win_h * SPLIT_FACE_ANCHOR_Y))
        x1 = max(0, min(x1, src_w - win_w))
        y1 = max(0, min(y1, src_h - win_h))
        windows.append((x1, y1, x1 + win_w, y1 + win_h))
    return windows


def inherit_split_centers(strategies, split_centers):
    """Validate that every SPLIT shot owns centers measured in that shot.

    Crop coordinates must never be copied across source cuts: another camera
    angle can place entirely different people at those coordinates.  A SPLIT
    decision without local centers safely downgrades to GENERAL.
    """
    strategies = list(strategies)
    centers = list(split_centers) + [None] * (len(strategies) - len(split_centers))
    for i, strategy in enumerate(strategies):
        if strategy == "SPLIT" and not centers[i]:
            strategies[i] = "GENERAL"
    return strategies, centers
