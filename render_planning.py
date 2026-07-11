"""
Scene layout planning for the vertical renderer.

Per detected scene the renderer picks a layout: TRACK (zoom on one person),
SPLIT (two people cropped individually and stacked — the Opus-Clip podcast
look) or GENERAL (blurred-background wide layout). These helpers hold the
decision logic, the split-crop geometry and the smoothing that stops the
layout from flickering between camera cuts. Stdlib-only so everything stays
unit-testable without OpenCV.
"""

MIN_SCENE_SECONDS = 1.5

# SPLIT requirements: two faces in most samples, horizontally separated by at
# least this fraction of the frame width (otherwise the two crops would show
# nearly the same image twice).
SPLIT_MIN_SEPARATION_FRACTION = 0.15
# Face sits slightly above panel center for natural headroom.
SPLIT_FACE_ANCHOR_Y = 0.42


def smooth_scene_strategies(strategies, scene_durations, min_scene_seconds=MIN_SCENE_SECONDS):
    """Stabilize per-scene TRACK/GENERAL decisions.

    1) Scenes shorter than min_scene_seconds inherit the previous scene's
       strategy — a sub-2s reaction cut should not flip the whole layout.
    2) Single-scene islands (X surrounded by the same Y on both sides) are
       flattened to Y — one odd detection result should not cause a
       layout round trip.
    """
    if not strategies:
        return []
    result = list(strategies)
    durations = list(scene_durations) + [min_scene_seconds] * (len(result) - len(scene_durations))

    for i in range(1, len(result)):
        if durations[i] < min_scene_seconds:
            result[i] = result[i - 1]

    for i in range(1, len(result) - 1):
        if result[i] != result[i - 1] and result[i - 1] == result[i + 1]:
            result[i] = result[i - 1]

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


def decide_scene_layout(face_samples, frame_width, layout_style="smart", person_samples=None):
    """Pick the layout for one scene from sampled detections.

    face_samples: one list of (x, y, w, h) face boxes per sampled frame.
    person_samples: optional matching lists of full-person YOLO boxes — the
    robust signal on wide shots, where the short-range face model misses
    distant or profile faces entirely.
    layout_style: "smart" (split two-person shots), "zoom" (legacy
    TRACK/GENERAL behavior) or "wide" (always the blurred wide layout).

    Returns (strategy, split_centers): strategy is 'TRACK' | 'GENERAL' |
    'SPLIT'; split_centers is [(x, y), (x, y)] left-to-right for SPLIT.
    """
    samples = [s for s in face_samples if s is not None]
    counts = [len(s) for s in samples]
    avg = (sum(counts) / len(counts)) if counts else 0.0

    if layout_style == "wide":
        return "GENERAL", None
    if layout_style == "zoom":
        # Legacy behavior: groups and empty shots go wide, single goes zoom.
        if avg > 1.2 or avg < 0.5:
            return "GENERAL", None
        return "TRACK", None

    # --- smart ---
    p_samples = [s for s in (person_samples or []) if s is not None]
    n_people = _median([len(s) for s in p_samples]) if p_samples else None

    if avg > 2.5 or (n_people is not None and n_people >= 3):
        # Three or more people: a two-panel split would drop someone.
        return "GENERAL", None

    if n_people == 2 or avg >= 1.5:
        # Two people in the shot: try the stacked split look. Prefer face
        # positions; fall back to head points of the person boxes when the
        # face detector misses them (typical for wide podcast shots).
        two_face_samples = [s for s in samples if len(s) >= 2]
        centers = None
        if two_face_samples and len(two_face_samples) * 2 >= max(1, len(samples)):
            centers = _median_pair_centers([_two_largest_centers(s) for s in two_face_samples])
        else:
            two_person_samples = [s for s in p_samples if len(s) >= 2]
            if two_person_samples:
                pairs = []
                for s in two_person_samples:
                    largest = sorted(s, key=lambda b: b[2] * b[3], reverse=True)[:2]
                    pairs.append(sorted((person_head_center(b) for b in largest), key=lambda c: c[0]))
                centers = _median_pair_centers(pairs)
        if centers and centers[1][0] - centers[0][0] >= frame_width * SPLIT_MIN_SEPARATION_FRACTION:
            return "SPLIT", [centers[0], centers[1]]
        return "GENERAL", None

    if avg < 0.5 and (n_people is None or n_people < 1):
        return "GENERAL", None
    return "TRACK", None


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
    """After smoothing, scenes may carry SPLIT without their own face centers
    (they inherited the label from a neighbor). Fill those from the nearest
    SPLIT scene that has centers — same people, same positions — and downgrade
    to GENERAL when no donor exists. Returns (strategies, split_centers)."""
    strategies = list(strategies)
    centers = list(split_centers) + [None] * (len(strategies) - len(split_centers))
    known = [c for s, c in zip(strategies, centers) if s == "SPLIT" and c]

    last_seen = None
    for i, strategy in enumerate(strategies):
        if strategy != "SPLIT":
            continue
        if centers[i]:
            last_seen = centers[i]
        elif last_seen:
            centers[i] = last_seen
        elif known:
            centers[i] = known[0]
        else:
            strategies[i] = "GENERAL"
    return strategies, centers
