from render_planning import smooth_scene_strategies


class TestSmoothSceneStrategies:
    def test_empty(self):
        assert smooth_scene_strategies([], []) == []

    def test_stable_plan_untouched(self):
        strategies = ["TRACK", "TRACK", "GENERAL", "GENERAL"]
        durations = [5.0, 4.0, 6.0, 3.0]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_short_scene_inherits_previous(self):
        # A 0.8s reaction cut must not flip the layout.
        strategies = ["TRACK", "GENERAL", "TRACK"]
        durations = [5.0, 0.8, 5.0]
        assert smooth_scene_strategies(strategies, durations) == ["TRACK", "TRACK", "TRACK"]

    def test_single_island_flattened(self):
        # One odd detection between two agreeing neighbors is noise.
        strategies = ["GENERAL", "TRACK", "GENERAL"]
        durations = [4.0, 4.0, 4.0]
        assert smooth_scene_strategies(strategies, durations) == ["GENERAL", "GENERAL", "GENERAL"]

    def test_real_layout_change_survives(self):
        # A sustained switch (long scenes, consistent) must be kept.
        strategies = ["TRACK", "TRACK", "GENERAL", "GENERAL", "GENERAL"]
        durations = [4.0, 4.0, 8.0, 5.0, 6.0]
        assert smooth_scene_strategies(strategies, durations) == strategies

    def test_rapid_interview_cuts_calm_down(self):
        # Wide shot <-> close-up ping-pong with short cuts collapses into one layout.
        strategies = ["GENERAL", "TRACK", "GENERAL", "TRACK", "GENERAL"]
        durations = [3.0, 1.0, 1.2, 0.9, 1.4]
        assert smooth_scene_strategies(strategies, durations) == ["GENERAL"] * 5

    def test_missing_durations_are_tolerated(self):
        strategies = ["TRACK", "GENERAL", "GENERAL"]
        assert smooth_scene_strategies(strategies, [4.0]) == ["TRACK", "GENERAL", "GENERAL"]

    def test_first_scene_never_inherits(self):
        strategies = ["GENERAL", "TRACK"]
        durations = [0.5, 9.0]
        assert smooth_scene_strategies(strategies, durations) == ["GENERAL", "TRACK"]


from render_planning import decide_scene_layout, inherit_split_centers, split_crop_windows


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
    def test_inherited_split_gets_neighbor_centers(self):
        strategies = ["SPLIT", "SPLIT", "TRACK"]
        centers = [[(100, 200), (900, 200)], None, None]
        out_strats, out_centers = inherit_split_centers(strategies, centers)
        assert out_strats == ["SPLIT", "SPLIT", "TRACK"]
        assert out_centers[1] == [(100, 200), (900, 200)]

    def test_split_without_any_donor_downgrades(self):
        out_strats, out_centers = inherit_split_centers(["SPLIT", "TRACK"], [None, None])
        assert out_strats == ["GENERAL", "TRACK"]

    def test_backward_donor_used(self):
        strategies = ["SPLIT", "SPLIT"]
        centers = [None, [(300, 100), (1200, 100)]]
        out_strats, out_centers = inherit_split_centers(strategies, centers)
        assert out_strats == ["SPLIT", "SPLIT"]
        assert out_centers[0] == [(300, 100), (1200, 100)]
