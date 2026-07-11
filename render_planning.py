"""
Scene-strategy smoothing for the vertical renderer.

analyze_scenes_strategy() decides TRACK (zoom on one person) or GENERAL
(blurred-background wide layout) per detected scene. Interview footage with
frequent camera cuts made that decision flip on almost every cut — the clip
"wobbled" between layouts. These helpers stabilize the plan; stdlib-only so
it stays unit-testable without OpenCV.
"""

MIN_SCENE_SECONDS = 1.5


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
