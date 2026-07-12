from speaker_tracking import SpeakerTracker, TargetState


def _face(x, y=100, width=120, height=120):
    return {"box": [x, y, width, height], "score": width * height}


def _person(x, y=100, width=400, height=700):
    return [x, y, width, height]


class TestSpeakerTrackerStates:
    def test_short_face_dropout_is_hold_not_lost(self):
        tracker = SpeakerTracker(lost_timeout_seconds=2.0)
        assert tracker.get_target([_face(100)], 0.0, 1920).state == TargetState.TARGET
        assert tracker.get_target([], 0.5, 1920).state == TargetState.HOLD
        assert tracker.get_target([], 1.99, 1920).state == TargetState.HOLD
        assert tracker.get_target([], 2.01, 1920).state == TargetState.LOST

    def test_active_face_wins_even_when_another_face_is_larger(self):
        tracker = SpeakerTracker()
        tracker.get_target([_face(100)], 0.0, 1920)
        decision = tracker.get_target(
            [_face(102), _face(1300, width=300, height=300)],
            0.5,
            1920,
        )
        assert decision.state == TargetState.TARGET
        assert decision.box[0] == 102.0

    def test_replacement_needs_seconds_of_stability_and_cooldown(self):
        tracker = SpeakerTracker(
            stabilization_seconds=2.0,
            cooldown_seconds=3.0,
            lost_timeout_seconds=2.0,
        )
        tracker.get_target([_face(100)], 0.0, 1920)
        assert tracker.get_target([_face(1300)], 0.2, 1920).state == TargetState.HOLD
        assert tracker.get_target([_face(1300)], 1.0, 1920).state == TargetState.HOLD
        assert tracker.get_target([_face(1300)], 2.3, 1920).state == TargetState.HOLD
        decision = tracker.get_target([_face(1300)], 3.1, 1920)
        assert decision.state == TargetState.TARGET
        assert decision.box[0] == 1300.0

    def test_replacement_visibility_gap_restarts_stabilization(self):
        tracker = SpeakerTracker(
            stabilization_seconds=2.0,
            cooldown_seconds=3.0,
            lost_timeout_seconds=2.0,
            identity_memory_seconds=10.0,
        )
        tracker.get_target([_face(100)], 0.0, 1920)
        assert tracker.get_target([_face(1300)], 3.0, 1920).state == TargetState.HOLD

        assert tracker.get_target([], 4.9, 1920).state == TargetState.LOST
        assert tracker.pending_switch_id is None
        assert tracker.pending_switch_since is None

        # The same remembered face returns, but the 1.9-second absence must
        # not count toward the required two seconds of continuous dominance.
        assert tracker.get_target([_face(1300)], 5.0, 1920).state == TargetState.HOLD
        assert tracker.get_target([_face(1300)], 5.2, 1920).state == TargetState.HOLD
        assert tracker.get_target([_face(1300)], 7.01, 1920).state == TargetState.TARGET

    def test_reset_drops_all_cross_scene_identity(self):
        tracker = SpeakerTracker()
        tracker.get_target([_face(100)], 0.0, 1920)
        assert tracker.has_target
        tracker.reset()
        assert not tracker.has_target
        assert tracker.active_speaker_id is None
        assert tracker.get_target([_face(1300)], 10.0, 1920).state == TargetState.TARGET


class TestYoloFallback:
    def test_initial_fallback_can_acquire_once(self):
        tracker = SpeakerTracker()
        assert tracker.get_target([], 0.0, 1920).state == TargetState.LOST
        decision = tracker.consider_person_fallback(_person(700), 0.0, 1920)
        assert decision.state == TargetState.TARGET
        assert tracker.get_target([], 0.5, 1920).state == TargetState.HOLD

    def test_existing_target_requires_stable_fallback(self):
        tracker = SpeakerTracker(
            lost_timeout_seconds=2.0,
            fallback_stabilization_seconds=1.0,
        )
        tracker.get_target([_face(100)], 0.0, 1920)
        assert tracker.get_target([], 2.1, 1920).state == TargetState.LOST
        assert tracker.consider_person_fallback(_person(1200), 2.1, 1920).state == TargetState.HOLD
        assert tracker.consider_person_fallback(_person(1205), 2.7, 1920).state == TargetState.HOLD
        decision = tracker.consider_person_fallback(_person(1198), 3.2, 1920)
        assert decision.state == TargetState.TARGET
        assert decision.box[0] == 1198.0

    def test_moving_fallback_candidate_restarts_confirmation(self):
        tracker = SpeakerTracker(fallback_stabilization_seconds=1.0)
        tracker.get_target([_face(100)], 0.0, 1920)
        tracker.get_target([], 2.1, 1920)
        assert tracker.consider_person_fallback(_person(1200), 2.1, 1920).state == TargetState.HOLD
        assert tracker.consider_person_fallback(_person(500), 3.2, 1920).state == TargetState.HOLD
        assert tracker.consider_person_fallback(_person(505), 3.8, 1920).state == TargetState.HOLD
