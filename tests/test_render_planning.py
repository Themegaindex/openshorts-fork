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
