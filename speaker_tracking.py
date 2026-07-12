"""Time-based, conservative camera target tracking.

The tracker deliberately distinguishes a temporary detector dropout (HOLD)
from a genuinely lost subject (LOST).  Callers must not run a fallback while
the state is HOLD; doing so was the source of rapid left/right camera cuts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional, Sequence


Box = tuple[float, float, float, float]


class TargetState(str, Enum):
    TARGET = "TARGET"
    HOLD = "HOLD"
    LOST = "LOST"


@dataclass(frozen=True)
class TargetDecision:
    state: TargetState
    box: Optional[Box] = None
    reason: str = ""


def _as_box(box: Sequence[float]) -> Box:
    x, y, width, height = box
    return float(x), float(y), float(width), float(height)


def _center(box: Sequence[float]) -> tuple[float, float]:
    x, y, width, height = box
    return float(x) + float(width) / 2.0, float(y) + float(height) / 2.0


class SpeakerTracker:
    """Hold one visual subject unless a replacement stays valid over time.

    This is intentionally not called an active-speaker detector: face size is
    not speech evidence.  In Smart mode, multi-person shots should already be
    SPLIT/GENERAL; TRACK therefore prioritizes a calm, persistent crop.
    """

    def __init__(
        self,
        stabilization_seconds: float = 2.0,
        cooldown_seconds: float = 3.0,
        lost_timeout_seconds: float = 2.0,
        fallback_stabilization_seconds: float = 1.0,
        identity_memory_seconds: float = 2.0,
    ):
        self.stabilization_seconds = max(0.0, float(stabilization_seconds))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.lost_timeout_seconds = max(0.0, float(lost_timeout_seconds))
        self.fallback_stabilization_seconds = max(0.0, float(fallback_stabilization_seconds))
        self.identity_memory_seconds = max(0.0, float(identity_memory_seconds))
        self.reset()

    def reset(self):
        self.active_speaker_id = None
        self.next_id = 0
        self.known_faces = []
        self.last_target_box = None
        self.last_target_at = None
        self.active_last_seen_at = None
        self.last_switch_at = -math.inf
        self.pending_switch_id = None
        self.pending_switch_since = None
        self.fallback_active = False
        self.fallback_candidate_center = None
        self.fallback_candidate_since = None

    @property
    def has_target(self):
        return self.last_target_box is not None

    def _match_faces(self, face_candidates, timestamp_seconds, frame_width):
        now = float(timestamp_seconds)
        max_age = self.identity_memory_seconds
        self.known_faces = [
            known for known in self.known_faces
            if now - known["last_seen_at"] <= max_age
        ]

        matched = []
        used_ids = set()
        max_distance = max(float(frame_width) * 0.15, 1.0)

        for face in face_candidates:
            box = _as_box(face["box"])
            center_x, center_y = _center(box)
            best = None
            best_distance = max_distance
            for known in self.known_faces:
                if known["id"] in used_ids:
                    continue
                distance = math.hypot(center_x - known["center_x"], center_y - known["center_y"])
                if distance < best_distance:
                    best = known
                    best_distance = distance

            if best is None:
                face_id = self.next_id
                self.next_id += 1
            else:
                face_id = best["id"]

            used_ids.add(face_id)
            self.known_faces = [known for known in self.known_faces if known["id"] != face_id]
            self.known_faces.append({
                "id": face_id,
                "center_x": center_x,
                "center_y": center_y,
                "last_seen_at": now,
            })
            matched.append({
                "id": face_id,
                "box": box,
                "score": float(face.get("score", box[2] * box[3])),
            })
        return matched

    def _activate(self, candidate, timestamp_seconds, reason):
        now = float(timestamp_seconds)
        self.active_speaker_id = candidate["id"]
        self.active_last_seen_at = now
        self.last_target_at = now
        self.last_target_box = candidate["box"]
        self.last_switch_at = now
        self.pending_switch_id = None
        self.pending_switch_since = None
        self.fallback_active = False
        self.fallback_candidate_center = None
        self.fallback_candidate_since = None
        return TargetDecision(TargetState.TARGET, candidate["box"], reason)

    def _hold(self, reason):
        return TargetDecision(TargetState.HOLD, None, reason)

    def _lost(self, reason):
        return TargetDecision(TargetState.LOST, None, reason)

    def get_target(self, face_candidates, timestamp_seconds, frame_width):
        """Return TARGET, HOLD, or LOST for the current face observation."""
        now = float(timestamp_seconds)
        candidates = self._match_faces(face_candidates, now, frame_width)

        if self.active_speaker_id is None:
            if self.fallback_active and self.last_target_box is not None:
                if candidates:
                    last_x, last_y = _center(self.last_target_box)
                    nearest = min(
                        candidates,
                        key=lambda candidate: math.hypot(
                            _center(candidate["box"])[0] - last_x,
                            _center(candidate["box"])[1] - last_y,
                        ),
                    )
                    nearest_x, nearest_y = _center(nearest["box"])
                    if math.hypot(nearest_x - last_x, nearest_y - last_y) <= max(float(frame_width) * 0.20, 1.0):
                        return self._activate(nearest, now, "face matched the held YOLO target")
                if self.last_target_at is not None and now - self.last_target_at < self.lost_timeout_seconds:
                    return self._hold("holding the YOLO-acquired target")
                return self._lost("YOLO-acquired target timed out")

            if not candidates:
                return self._lost("no target has been acquired")
            best = max(candidates, key=lambda candidate: candidate["score"])
            return self._activate(best, now, "initial face target")

        active = next(
            (candidate for candidate in candidates if candidate["id"] == self.active_speaker_id),
            None,
        )
        if active is not None:
            self.active_last_seen_at = now
            self.last_target_at = now
            self.last_target_box = active["box"]
            self.pending_switch_id = None
            self.pending_switch_since = None
            return TargetDecision(TargetState.TARGET, active["box"], "active face confirmed")

        elapsed_since_seen = (
            math.inf if self.active_last_seen_at is None
            else now - self.active_last_seen_at
        )
        if not candidates:
            # A replacement must be visible continuously for the full
            # stabilization interval. Detector gaps cannot count as evidence.
            self.pending_switch_id = None
            self.pending_switch_since = None
            if elapsed_since_seen < self.lost_timeout_seconds:
                return self._hold("active face briefly missing")
            return self._lost("active face lost beyond timeout")

        # A different face may be present, but that is not proof of a speaker
        # switch.  It must remain the same candidate for the full stability
        # interval, while the previous target is genuinely lost.
        candidate = max(candidates, key=lambda item: item["score"])
        if candidate["id"] != self.pending_switch_id:
            self.pending_switch_id = candidate["id"]
            self.pending_switch_since = now

        pending_seconds = now - float(self.pending_switch_since)
        cooldown_elapsed = now - self.last_switch_at >= self.cooldown_seconds
        old_target_lost = elapsed_since_seen >= self.lost_timeout_seconds
        stable = pending_seconds >= self.stabilization_seconds
        if old_target_lost and stable and cooldown_elapsed:
            return self._activate(candidate, now, "replacement face stayed stable")
        return self._hold("replacement face is not confirmed")

    def consider_person_fallback(self, person_box, timestamp_seconds, frame_width):
        """Acquire a YOLO person only from LOST, never from HOLD.

        Initial acquisition is immediate so a face-detector miss does not leave
        a new single-person shot centered on empty space.  Replacing an existing
        target requires a spatially stable YOLO candidate first.
        """
        now = float(timestamp_seconds)
        if person_box is None:
            self.fallback_candidate_center = None
            self.fallback_candidate_since = None
            return self._lost("YOLO found no fallback person")

        box = _as_box(person_box)
        center = _center(box)
        if not self.has_target:
            self.last_target_box = box
            self.last_target_at = now
            self.last_switch_at = now
            self.fallback_active = True
            return TargetDecision(TargetState.TARGET, box, "initial YOLO fallback target")

        previous_center = self.fallback_candidate_center
        consistent = (
            previous_center is not None
            and math.hypot(center[0] - previous_center[0], center[1] - previous_center[1])
            <= max(float(frame_width) * 0.10, 1.0)
        )
        if not consistent:
            self.fallback_candidate_center = center
            self.fallback_candidate_since = now
            return self._hold("waiting for a stable YOLO fallback")

        self.fallback_candidate_center = center
        stable_for = now - float(self.fallback_candidate_since)
        if stable_for < self.fallback_stabilization_seconds:
            return self._hold("waiting for a stable YOLO fallback")

        self.active_speaker_id = None
        self.active_last_seen_at = None
        self.last_target_box = box
        self.last_target_at = now
        self.last_switch_at = now
        self.pending_switch_id = None
        self.pending_switch_since = None
        self.fallback_active = True
        self.fallback_candidate_center = None
        self.fallback_candidate_since = None
        return TargetDecision(TargetState.TARGET, box, "stable YOLO fallback target")
