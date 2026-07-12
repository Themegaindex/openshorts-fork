from render_planning import smooth_scene_strategies


class TestSmoothSceneStrategies:
    def test_empty(self):
        assert smooth_scene_strategies([], []) == []

    def test_stable_plan_untouched(self):
        strategies = ["TRACK", "TRACK", "GENERAL", "GENERAL"]
        durations = [5.0, 4.0, 6.0, 3.0]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_short_confident_source_cut_survives(self):
        # Short reaction shots are legitimate edits when detection is clear.
        strategies = ["TRACK", "GENERAL", "TRACK"]
        durations = [5.0, 0.8, 5.0]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_long_single_island_is_never_flattened(self):
        strategies = ["GENERAL", "TRACK", "GENERAL"]
        durations = [4.0, 4.0, 4.0]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_short_low_confidence_island_widens_safely(self):
        strategies = ["GENERAL", "TRACK", "GENERAL"]
        durations = [4.0, 0.8, 4.0]
        confidences = [0.9, 0.2, 0.9]
        assert smooth_scene_strategies(strategies, durations, confidences) == ["GENERAL"] * 3

    def test_real_layout_change_survives(self):
        # A sustained switch (long scenes, consistent) must be kept.
        strategies = ["TRACK", "TRACK", "GENERAL", "GENERAL", "GENERAL"]
        durations = [4.0, 4.0, 8.0, 5.0, 6.0]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_confident_interview_cuts_are_preserved(self):
        strategies = ["GENERAL", "TRACK", "GENERAL", "TRACK", "GENERAL"]
        durations = [3.0, 1.0, 1.2, 0.9, 1.4]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_clip_10_long_split_regression(self):
        strategies = ["TRACK", "TRACK", "SPLIT", "TRACK", "SPLIT"]
        durations = [0.417, 1.635, 27.494, 2.386, 7.191]
        confidences = [1.0, 1.0, 1.0, 1.0, 1.0]
        assert smooth_scene_strategies(strategies, durations, confidences) == strategies

    def test_missing_durations_are_tolerated(self):
        strategies = ["TRACK", "GENERAL", "GENERAL"]
        assert smooth_scene_strategies(strategies, [4.0]) == ["TRACK", "GENERAL", "GENERAL"]

    def test_first_scene_never_inherits(self):
        strategies = ["GENERAL", "TRACK"]
        durations = [0.5, 9.0]
        assert smooth_scene_strategies(strategies, durations) == ["GENERAL", "TRACK"]


from render_planning import (
    decide_scene_layout,
    decide_scene_layout_detailed,
    inherit_split_centers,
    sample_scene_frames,
    split_crop_windows,
)


def _face(x, y, w=100, h=100):
    return (x, y, w, h)


class TestDecideSceneLayout:
    def test_two_separated_people_split(self):
        samples = [[_face(200, 300), _face(1300, 320)]] * 3
        strategy, centers = decide_scene_layout(samples, 1920, layout_style="smart")
        assert strategy == "SPLIT"
        assert centers[0][0] < centers[1][0]  # left-to-right

    def test_two_people_too_close_falls_back_to_general(self):
        samples = [[_face(900, 300), _face(1050, 320)]] * 3  # 150px apart < 15% of 1920
        strategy, centers = decide_scene_layout(samples, 1920, layout_style="smart")
        assert strategy == "GENERAL" and centers is None

    def test_single_person_tracks(self):
        samples = [[_face(900, 300)]] * 3
        assert decide_scene_layout(samples, 1920, layout_style="smart")[0] == "TRACK"

    def test_no_faces_general(self):
        assert decide_scene_layout([[], [], []], 1920, layout_style="smart")[0] == "GENERAL"

    def test_three_people_general(self):
        samples = [[_face(200, 300), _face(900, 300), _face(1600, 300)]] * 3
        assert decide_scene_layout(samples, 1920, layout_style="smart")[0] == "GENERAL"

    def test_zoom_style_never_splits(self):
        samples = [[_face(200, 300), _face(1300, 320)]] * 3
        strategy, centers = decide_scene_layout(samples, 1920, layout_style="zoom")
        assert strategy == "GENERAL" and centers is None

    def test_wide_style_always_general(self):
        samples = [[_face(900, 300)]] * 3
        assert decide_scene_layout(samples, 1920, layout_style="wide")[0] == "GENERAL"

    def test_flaky_second_face_stays_track(self):
        # Second face only detected in 1 of 3 samples -> not a stable duo.
        samples = [[_face(900, 300)], [_face(900, 300), _face(1400, 300)], [_face(900, 300)]]
        assert decide_scene_layout(samples, 1920, layout_style="smart")[0] == "TRACK"


class TestSplitCropWindows:
    def test_stacked_windows_inside_source(self):
        centers = [(480, 540), (1440, 540)]
        windows = split_crop_windows(1920, 1080, 1080, 1920, centers, stacked=True)
        assert len(windows) == 2
        for (x1, y1, x2, y2) in windows:
            assert 0 <= x1 < x2 <= 1920
            assert 0 <= y1 < y2 <= 1080
            assert x2 - x1 <= 960  # capped at half the source width

    def test_stacked_aspect_matches_panel(self):
        windows = split_crop_windows(1920, 1080, 1080, 1920, [(480, 540), (1440, 540)], stacked=True)
        panel_aspect = 1080 / (1920 // 2)
        for (x1, y1, x2, y2) in windows:
            assert abs(((x2 - x1) / (y2 - y1)) - panel_aspect) < 0.05

    def test_side_by_side_for_square(self):
        windows = split_crop_windows(1920, 1080, 1080, 1080, [(480, 540), (1440, 540)], stacked=False)
        panel_aspect = (1080 // 2) / 1080
        for (x1, y1, x2, y2) in windows:
            assert abs(((x2 - x1) / (y2 - y1)) - panel_aspect) < 0.05

    def test_edge_faces_are_clamped(self):
        windows = split_crop_windows(1920, 1080, 1080, 1920, [(10, 10), (1910, 1070)], stacked=True)
        for (x1, y1, x2, y2) in windows:
            assert x1 >= 0 and y1 >= 0 and x2 <= 1920 and y2 <= 1080


class TestInheritSplitCenters:
    def test_split_without_local_centers_downgrades(self):
        strategies = ["SPLIT", "SPLIT", "TRACK"]
        centers = [[(100, 200), (900, 200)], None, None]
        out_strats, out_centers = inherit_split_centers(strategies, centers)
        assert out_strats == ["SPLIT", "GENERAL", "TRACK"]
        assert out_centers[1] is None

    def test_split_without_any_donor_downgrades(self):
        out_strats, out_centers = inherit_split_centers(["SPLIT", "TRACK"], [None, None])
        assert out_strats == ["GENERAL", "TRACK"]

    def test_centers_are_not_copied_backward_across_a_cut(self):
        strategies = ["SPLIT", "SPLIT"]
        centers = [None, [(300, 100), (1200, 100)]]
        out_strats, out_centers = inherit_split_centers(strategies, centers)
        assert out_strats == ["GENERAL", "SPLIT"]
        assert out_centers[0] is None


from render_planning import person_head_center


def _person(x, y, w=300, h=800):
    return (x, y, w, h)


class TestPersonHeadCenter:
    def test_head_is_top_fifth_centered(self):
        cx, cy = person_head_center((100, 200, 300, 800))
        assert cx == 250          # horizontally centered
        assert cy == 200 + 160    # 20% down the body


class TestDecideSceneLayoutWithPersons:
    def test_wide_shot_two_persons_no_faces_splits(self):
        # The podcast bug: face model sees nothing on the wide shot, but YOLO
        # reliably sees two people -> must SPLIT with head centers.
        faces = [[], [], [], [], []]
        persons = [[_person(200, 300), _person(1300, 320)]] * 5
        strategy, centers = decide_scene_layout(faces, 1920, layout_style="smart", person_samples=persons)
        assert strategy == "SPLIT"
        assert centers[0][0] < centers[1][0]
        assert centers[0] == person_head_center(_person(200, 300))

    def test_flaky_single_face_with_two_persons_splits(self):
        # Face detector occasionally catches one face — person count wins.
        faces = [[(250, 350, 90, 90)], [], [(250, 350, 90, 90)], [], []]
        persons = [[_person(200, 300), _person(1300, 320)]] * 5
        strategy, _ = decide_scene_layout(faces, 1920, layout_style="smart", person_samples=persons)
        assert strategy == "SPLIT"

    def test_single_person_no_face_tracks(self):
        faces = [[], [], []]
        persons = [[_person(800, 200)]] * 3
        assert decide_scene_layout(faces, 1920, layout_style="smart", person_samples=persons)[0] == "TRACK"

    def test_three_persons_general(self):
        faces = [[], [], []]
        persons = [[_person(100, 300), _person(800, 300), _person(1500, 300)]] * 3
        assert decide_scene_layout(faces, 1920, layout_style="smart", person_samples=persons)[0] == "GENERAL"

    def test_two_persons_too_close_general(self):
        faces = [[], [], []]
        persons = [[_person(800, 300), _person(950, 300)]] * 3  # heads ~150px apart
        assert decide_scene_layout(faces, 1920, layout_style="smart", person_samples=persons)[0] == "GENERAL"

    def test_nothing_detected_general(self):
        assert decide_scene_layout([[], [], []], 1920, layout_style="smart", person_samples=[[], [], []])[0] == "GENERAL"

    def test_faces_preferred_over_person_boxes_for_centers(self):
        # When both signals see two people, centers come from the faces.
        faces = [[(240, 340, 100, 100), (1340, 360, 100, 100)]] * 3
        persons = [[_person(200, 300), _person(1300, 320)]] * 3
        strategy, centers = decide_scene_layout(faces, 1920, layout_style="smart", person_samples=persons)
        assert strategy == "SPLIT"
        assert centers[0] == (290.0, 390.0)  # face center, not person head point

    def test_zoom_style_ignores_person_samples(self):
        faces = [[(250, 350, 90, 90)]] * 3
        persons = [[_person(200, 300), _person(1300, 320)]] * 3
        assert decide_scene_layout(faces, 1920, layout_style="zoom", person_samples=persons)[0] == "TRACK"

    def test_ambiguous_multi_person_evidence_goes_wide_not_track(self):
        faces = [[_face(900, 300)]] * 5
        persons = [
            [_person(800, 200)],
            [_person(800, 200)],
            [_person(800, 200)],
            [_person(200, 300), _person(1300, 320)],
            [_person(200, 300), _person(1300, 320)],
        ]
        decision = decide_scene_layout_detailed(
            faces,
            1920,
            layout_style="smart",
            person_samples=persons,
        )
        assert decision.strategy == "GENERAL"
        assert decision.confidence == 0.4


class TestSceneSampling:
    def test_long_scene_gets_more_than_five_samples(self):
        frames = sample_scene_frames(0, 1800, fps=60.0)
        assert len(frames) == 24
        assert frames == sorted(frames)
        assert all(0 <= frame < 1800 for frame in frames)

    def test_short_scene_still_gets_coverage(self):
        frames = sample_scene_frames(100, 220, fps=60.0)
        assert len(frames) == 5
        assert frames[0] >= 100 and frames[-1] < 220
