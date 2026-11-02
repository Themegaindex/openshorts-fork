import pytest

from speaker_tracking import (
    FallbackThrottle, SpeakerTracker, TargetState, effective_detect_stride, person_fallback_due,
)


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


@pytest.mark.parametrize(
    "fps,stride,expected",
    [
        (30, 4, 4),
        (25, 4, 4),
        (60, 4, 4),
        (10, 4, 2),        # capped to 0.25 s of video, never below 2
        (5, 4, 2),
        (30, 2, 2),        # legacy stride stays exact
        (30, 0, 1),
        (0, 4, 4),         # unknown fps: no cap
        (None, 4, 4),
        ("bad", 4, 4),
    ],
)
def test_effective_detect_stride(fps, stride, expected):
    assert effective_detect_stride(fps, stride) == expected


def test_fallback_throttle_runs_at_most_every_interval_and_resets_per_scene():
    throttle = FallbackThrottle(0.3)
    assert [throttle.should_run(t) for t in (0.0, 0.1, 0.2, 0.3, 0.5, 0.6)] == [
        True, False, False, True, False, True,
    ]
    throttle.reset()
    assert throttle.should_run(0.65) is True


def test_fallback_throttle_zero_interval_runs_on_every_tick():
    throttle = FallbackThrottle(0)
    assert all(throttle.should_run(t / 30) for t in range(10))


def _replay_lost_walker(speed_px_per_second, gate):
    """Replay the TRACK branch: face lost at 0.5 s, a person walks sideways."""
    fps, width = 30.0, 1920
    stride = effective_detect_stride(fps, 4)
    tracker = SpeakerTracker()
    throttle = FallbackThrottle(0.3)
    tracker.get_target([_face(900)], 0.0, width)
    for frame in range(stride, int(fps * 12), stride):
        now = frame / fps
        decision = tracker.get_target([], now, width)
        if decision.state == TargetState.LOST and gate(tracker, throttle, now):
            travel = (200 + speed_px_per_second * now) % (2 * (width - 400))
            x = travel if travel <= width - 400 else 2 * (width - 400) - travel   # bounce, no jump
            result = tracker.consider_person_fallback(_person(x), now, width)
            if result.reason == "stable YOLO fallback target":
                return now
    return None


@pytest.mark.parametrize("speed", [0, 300, 600, 1000])
def test_walking_person_is_reacquired_with_the_throttle(speed):
    reacquired = _replay_lost_walker(speed, person_fallback_due)
    assert reacquired is not None and reacquired < 4.0


def test_throttle_alone_would_never_confirm_a_fast_walker():
    assert _replay_lost_walker(600, lambda _t, throttle, now: throttle.should_run(now)) is None


def test_fallback_search_stays_throttled_while_nobody_is_found():
    tracker = SpeakerTracker()
    throttle = FallbackThrottle(0.3)
    runs = [person_fallback_due(tracker, throttle, t / 30) for t in range(0, 30, 4)]
    assert runs.count(True) == 3          # 0.0, 0.4 and 0.8 s, not every 0.133 s
