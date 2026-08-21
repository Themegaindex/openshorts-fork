import time
import cv2
import scenedetect
import subprocess
import argparse
import glob
import re
import sys
import math
import threading
from contextlib import contextmanager
from scenedetect import SceneManager
from scenedetect.detectors import ContentDetector
# PySceneDetect 0.7 removed VideoManager; 0.6+ provides open_video. Support
# both so fresh installs and existing venvs keep working.
try:
    from scenedetect import open_video
except ImportError:
    open_video = None
try:
    from scenedetect import VideoManager
except ImportError:
    VideoManager = None
from ultralytics import YOLO
import torch
import os
import numpy as np
from tqdm import tqdm
import yt_dlp
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
# import whisper (replaced by faster_whisper inside function)
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
import json
import shutil
from typing import List, Optional
import longform

# OS advisory file locks for the shared ETA history (_job_stats_file_lock):
# msvcrt on Windows, flock everywhere else.
if os.name == "nt":
    import msvcrt
else:
    import fcntl
from pydantic import BaseModel
from clip_selection import (
    build_transcript_windows,
    choose_distinct_clips,
    selection_limits,
    snap_clip_to_words,
)
from media_audio import extract_audio_track
from render_planning import (
    SceneLayoutDecision,
    decide_scene_layout_detailed,
    inherit_split_centers,
    sample_scene_frames,
    smooth_scene_strategies,
    split_crop_windows,
)
from speaker_tracking import SpeakerTracker, TargetState
from video_formats import (
    CANONICAL_OUTPUT_FORMATS,
    EVEN_PAD_FILTER,
    OUTPUT_FORMAT_CHOICES,
    fit_even_output_dimensions,
    normalize_output_format,
    output_aspect_ratio,
)

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module='google.protobuf')

# Load environment variables
load_dotenv()

# --- Constants ---
ASPECT_RATIO = 9 / 16
OUTPUT_FORMATS = CANONICAL_OUTPUT_FORMATS
LAYOUT_STYLES = ("smart", "zoom", "wide")
# Watermark: subtle centered overlay so rendered clips can't be re-uploaded as
# someone else's work. Configure via env; WATERMARK_TEXT wins over the image.
WATERMARK_ENABLED = os.environ.get("WATERMARK_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")
WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "").strip()
WATERMARK_IMAGE = os.environ.get("WATERMARK_IMAGE", "").strip()
try:
    WATERMARK_OPACITY = min(0.5, max(0.01, float(os.environ.get("WATERMARK_OPACITY", "0.08"))))
except ValueError:
    WATERMARK_OPACITY = 0.08
try:
    WATERMARK_WIDTH_FRACTION = min(0.9, max(0.1, float(os.environ.get("WATERMARK_WIDTH_FRACTION", "0.45"))))
except ValueError:
    WATERMARK_WIDTH_FRACTION = 0.45
GEMINI_MAX_ATTEMPTS = 3
EVENT_PREFIX = "__JOB_EVENT__"
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("JOB_HEARTBEAT_INTERVAL_SECONDS", "5"))
GEMINI_SLOW_WARNING_SECONDS = int(os.environ.get("GEMINI_SLOW_WARNING_SECONDS", "180"))
GEMINI_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_REQUEST_TIMEOUT_SECONDS", "600"))
GEMINI_MAX_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_MAX_TIMEOUT_SECONDS", "900"))
GEMINI_REQUEST_TIMEOUT_SECONDS = min(GEMINI_REQUEST_TIMEOUT_SECONDS, GEMINI_MAX_TIMEOUT_SECONDS)
GEMINI_REQUEST_WATCHDOG_GRACE_SECONDS = max(
    10,
    int(os.environ.get("GEMINI_REQUEST_WATCHDOG_GRACE_SECONDS", "30")),
)
GEMINI_WINDOW_SECONDS = int(os.environ.get("GEMINI_WINDOW_SECONDS", "90"))
GEMINI_WINDOW_OVERLAP_SECONDS = int(os.environ.get("GEMINI_WINDOW_OVERLAP_SECONDS", "30"))
# Analysis model, overridable per task (GEMINI_MODEL_ANALYSIS) or globally (GEMINI_MODEL).
GEMINI_ANALYSIS_MODEL = (
    os.environ.get("GEMINI_MODEL_ANALYSIS")
    or os.environ.get("GEMINI_MODEL")
    or "gemini-3-flash-preview"
)
GEMINI_SCORE_BATCH_SIZE = int(os.environ.get("GEMINI_SCORE_BATCH_SIZE", "8"))
GEMINI_DETAIL_BATCH_SIZE = int(os.environ.get("GEMINI_DETAIL_BATCH_SIZE", "4"))
GEMINI_SHORTLIST_LIMIT = int(os.environ.get("GEMINI_SHORTLIST_LIMIT", "10"))
GEMINI_LONG_VIDEO_SECONDS = float(os.environ.get("GEMINI_LONG_VIDEO_SECONDS", "7200"))
GEMINI_LONG_SHORTLIST_LIMIT = int(os.environ.get("GEMINI_LONG_SHORTLIST_LIMIT", "15"))
GEMINI_MAX_CLIPS = int(os.environ.get("GEMINI_MAX_CLIPS", "10"))
GEMINI_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_worker.py")
LONGFORM_TARGET_MIN_SECONDS = float(os.environ.get("LONGFORM_TARGET_MIN_SECONDS", "480"))
LONGFORM_TARGET_MAX_SECONDS = float(os.environ.get("LONGFORM_TARGET_MAX_SECONDS", "600"))
# Unlike GEMINI_LONG_VIDEO_SECONDS (a two-hour Shorts shortlist threshold), this
# controls when Smart/Auto may attempt an 8-10 minute long-form edit.
LONGFORM_MIN_SOURCE_SECONDS = float(os.environ.get("LONGFORM_MIN_SOURCE_SECONDS", "540"))
LONGFORM_HARD_MIN_SOURCE_SECONDS = float(os.environ.get("LONGFORM_HARD_MIN_SOURCE_SECONDS", "240"))
LONGFORM_MIN_SEGMENT_SECONDS = float(os.environ.get("LONGFORM_MIN_SEGMENT_SECONDS", "20"))
LONGFORM_MAX_SEGMENT_SECONDS = float(os.environ.get("LONGFORM_MAX_SEGMENT_SECONDS", "240"))
LONGFORM_MERGE_GAP_SECONDS = float(os.environ.get("LONGFORM_MERGE_GAP_SECONDS", "4"))
LONGFORM_MAX_SEGMENTS = int(os.environ.get("LONGFORM_MAX_SEGMENTS", "30"))
LONGFORM_COLD_OPEN = os.environ.get("LONGFORM_COLD_OPEN", "1").strip().lower() not in ("0", "false", "off", "no")
LONGFORM_COLD_OPEN_MAX_SECONDS = float(os.environ.get("LONGFORM_COLD_OPEN_MAX_SECONDS", "20"))
LONGFORM_AUDIO_FADE_SECONDS = float(os.environ.get("LONGFORM_AUDIO_FADE_SECONDS", "0.04"))
LONGFORM_CANVAS_WIDTH = int(os.environ.get("LONGFORM_CANVAS_WIDTH", "1920"))
LONGFORM_CANVAS_HEIGHT = int(os.environ.get("LONGFORM_CANVAS_HEIGHT", "1080"))
GEMINI_LONGFORM_TIMEOUT_SECONDS = min(
    int(os.environ.get("GEMINI_LONGFORM_TIMEOUT_SECONDS", str(GEMINI_REQUEST_TIMEOUT_SECONDS))),
    GEMINI_MAX_TIMEOUT_SECONDS,
)
PHASE_RANGES = {
    "queued": (0.0, 2.0),
    "download": (2.0, 20.0),
    "transcribe": (20.0, 55.0),
    "analyze": (55.0, 78.0),
    "render": (78.0, 95.0),
    "finalize": (95.0, 100.0),
    "completed": (100.0, 100.0),
}

PHASE_ETA_ORDER = ["download", "transcribe", "analyze", "render", "finalize"]

# Cross-job history was dropped once because it stored transcribe as a ratio and
# analyze as absolute seconds in the same list, so a full render and a short-clip
# job produced incomparable numbers. Pinning one normalising unit per phase makes
# that history meaningful again: every phase is measured in the quantity it
# actually scales with.
PHASE_COST_UNITS = {
    "download": "per_source_second",
    "transcribe": "per_source_second",
    "analyze": "per_source_second_after_overhead",
    "render": "per_output_second",
    "finalize": "absolute",
}
# Seeds until the job has measured its own numbers (see .job_stats.json).
PHASE_COST_PRIORS = {
    "download": 0.03,
    "transcribe": 0.50,
    "analyze": 0.02,
    "render": 1.20,
    "finalize": 5.0,
}
# Fixed overhead every phase pays regardless of length (model load, API round
# trips, container mux), so a 30s video does not get a 1-second estimate.
PHASE_COST_FLOOR = {
    "download": 5.0,
    "transcribe": 15.0,
    "analyze": 30.0,
    "render": 10.0,
    "finalize": 2.0,
}
PHASE_FIXED_OVERHEAD = {
    "analyze": 30.0,
}
# The clip count is unknown until the analysis finishes; assume a typical result
# so the total ETA does not start out far too low.
ASSUMED_OUTPUT_SECONDS = 8 * 40.0
JOB_STATS_SAMPLE_LIMIT = 20
JOB_STATS_LOCK_TIMEOUT_SECONDS = 10.0
BLOCKING_OPERATION_STALL_SECONDS = int(os.environ.get("JOB_BLOCKING_STALL_SECONDS", "1800"))
YTDLP_PROBE_STALL_SECONDS = int(os.environ.get("JOB_YTDLP_PROBE_STALL_SECONDS", "600"))
YTDLP_REFUSAL_BACKOFF_SECONDS = int(os.environ.get("JOB_YTDLP_REFUSAL_BACKOFF_SECONDS", "20"))
MERGE_SECONDS_PER_SOURCE_SECOND = float(os.environ.get("JOB_MERGE_SECONDS_PER_SOURCE_SECOND", "0.015"))
MERGE_MIN_SECONDS = float(os.environ.get("JOB_MERGE_MIN_SECONDS", "30"))
MERGE_MAX_SECONDS = float(os.environ.get("JOB_MERGE_MAX_SECONDS", "900"))
MERGE_MIN_STALL_SECONDS = int(os.environ.get("JOB_MERGE_MIN_STALL_SECONDS", "240"))
MERGE_STALL_MULTIPLIER = float(os.environ.get("JOB_MERGE_STALL_MULTIPLIER", "4"))
JOB_STATS_PATH = os.environ.get("JOB_STATS_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", ".job_stats.json"
)


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    mid = count // 2
    return ordered[mid] if count % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _load_job_stats() -> dict:
    """Measured phase costs of previous jobs, normalised per PHASE_COST_UNITS."""
    try:
        with open(JOB_STATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@contextmanager
def _job_stats_file_lock(timeout_seconds=JOB_STATS_LOCK_TIMEOUT_SECONDS):
    """Cross-process lock for the shared learned-ETA history.

    Workers are separate Python processes, so a threading lock cannot protect
    their common JSON file. The kernel's advisory lock (msvcrt.locking/flock)
    is held on a persistent sidecar file: only one holder can ever exist, and
    the OS releases the lock the moment its holder dies, so no stale-lock
    heuristics are needed. The sidecar is deliberately never deleted —
    unlinking a locked path would let the next process lock a fresh file
    while the previous holder still owns the old one.
    """
    lock_path = f"{JOB_STATS_PATH}.lock"
    os.makedirs(os.path.dirname(JOB_STATS_PATH) or ".", exist_ok=True)
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the job-stats lock")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _append_job_stat_samples(samples: dict) -> dict:
    """Merge measured cost samples into the shared history and persist it.

    ``samples`` maps phase -> {unit: [values]}. The re-read happens while the
    inter-process lock is held so concurrent completions (or a calibration
    run) cannot lose one another's samples. Returns the stored stats.
    """
    with _job_stats_file_lock():
        stats = _load_job_stats()
        for phase, units in samples.items():
            for unit, values in units.items():
                bucket = stats.setdefault(phase, {}).setdefault(unit, [])
                bucket.extend(round(float(value), 4) for value in values)
                del bucket[:-JOB_STATS_SAMPLE_LIMIT]
        tmp_path = (
            f"{JOB_STATS_PATH}.{os.getpid()}.{threading.get_ident()}."
            f"{time.time_ns()}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(stats, f)
            os.replace(tmp_path, JOB_STATS_PATH)
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
    return stats


def _estimated_merge_seconds(video_duration) -> float:
    """Conservative stream-copy estimate used while yt-dlp is muxing."""
    try:
        duration = max(0.0, float(video_duration or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    estimate = MERGE_MIN_SECONDS + duration * MERGE_SECONDS_PER_SOURCE_SECOND
    return max(MERGE_MIN_SECONDS, min(MERGE_MAX_SECONDS, estimate))


class JobReporter:
    def __init__(self, job_id: Optional[str] = None):
        self.job_id = job_id
        self.started_at = time.time()
        self.phase = "queued"
        self.phase_label = "Queued"
        self.phase_started_at = self.started_at
        self.phase_progress_percent = 0.0
        self.progress_percent = 0.0
        self.last_heartbeat_at = 0.0
        self.video_duration = None
        self.output_seconds = None
        self.phase_durations = {}
        self.job_stats = _load_job_stats()
        self.last_work_activity_at = self.started_at
        self.operation_name = None
        self.operation_deadline_at = None
        self.operation_expected_end_at = None
        self.operation_timeout_seconds = None
        self._state_lock = threading.RLock()

    def _overall_progress(self, phase: Optional[str] = None, phase_progress_percent: Optional[float] = None) -> float:
        current_phase = phase or self.phase
        phase_percent = self.phase_progress_percent if phase_progress_percent is None else phase_progress_percent
        start, end = PHASE_RANGES.get(current_phase, (self.progress_percent, self.progress_percent))
        phase_percent = max(0.0, min(100.0, float(phase_percent)))
        if end <= start:
            return max(0.0, min(100.0, end))
        return round(start + ((end - start) * (phase_percent / 100.0)), 2)

    def set_output_seconds(self, seconds):
        """Total seconds of rendered output — the unit the render phase scales with."""
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if value > 0:
            self.output_seconds = value

    def _phase_cost_prior(self, phase: str) -> Optional[float]:
        """Expected total seconds for a phase, from learned medians when available."""
        unit = PHASE_COST_UNITS.get(phase)
        if unit is None:
            return None
        samples = [
            value for value in (self.job_stats.get(phase) or {}).get(unit) or []
            if isinstance(value, (int, float)) and value >= 0
        ]
        rate = _median(samples)
        if rate is None:
            rate = PHASE_COST_PRIORS[phase]
        floor = PHASE_COST_FLOOR.get(phase, 0.0)
        if unit == "absolute":
            return max(floor, float(rate))
        if unit in {"per_source_second", "per_source_second_after_overhead"}:
            if not self.video_duration:
                return None
            if unit == "per_source_second_after_overhead":
                fixed = PHASE_FIXED_OVERHEAD.get(phase, floor)
                return max(floor, float(fixed) + float(rate) * self.video_duration)
            return max(floor, float(rate) * self.video_duration)
        return max(floor, float(rate) * (self.output_seconds or ASSUMED_OUTPUT_SECONDS))

    def _estimate_phase_remaining(self) -> Optional[float]:
        """Remaining seconds of the running phase.

        Blends the prior with this job's measured pace, weighted by how far the
        phase has come. Early on the prior carries the estimate, later the
        measurement does. A number is therefore available immediately instead of
        showing "calculating" for the first two minutes of every phase, and it
        does not jump when the measurement takes over.
        """
        if self.phase not in PHASE_ETA_ORDER:
            return None
        percent = max(0.0, min(100.0, self.phase_progress_percent))
        now = time.time()
        in_phase = max(0.0, now - self.phase_started_at)

        with self._state_lock:
            operation_expected_end_at = self.operation_expected_end_at
        if operation_expected_end_at is not None:
            return max(0.0, operation_expected_end_at - now)

        measured = None
        if percent >= 1.0 and in_phase >= 3.0:
            measured = max(0.0, (in_phase * 100.0 / percent) - in_phase)

        prior_total = self._phase_cost_prior(self.phase)
        prior_remaining = None
        if prior_total is not None:
            prior_remaining = max(0.0, prior_total * (1.0 - percent / 100.0))

        if measured is None:
            return prior_remaining
        if prior_remaining is None:
            return measured
        weight = percent / 100.0
        return (weight * measured) + ((1.0 - weight) * prior_remaining)

    def _estimate_total_remaining(self, phase_remaining: Optional[float]) -> Optional[float]:
        """Remaining seconds of the whole job: current phase plus every phase left."""
        if phase_remaining is None or self.phase not in PHASE_ETA_ORDER:
            return None
        total = float(phase_remaining)
        for phase in PHASE_ETA_ORDER[PHASE_ETA_ORDER.index(self.phase) + 1:]:
            prior = self._phase_cost_prior(phase)
            if prior is None:
                return None
            total += prior
        return total

    def _record_job_stats(self):
        """Persist this job's measured phase costs for future estimates."""
        if not self.phase_durations:
            return
        samples = {}
        for phase, seconds in self.phase_durations.items():
            if phase in getattr(self, "stats_excluded_phases", ()):
                continue
            unit = PHASE_COST_UNITS.get(phase)
            if unit is None or not seconds or seconds < 1.0:
                continue
            if unit in {"per_source_second", "per_source_second_after_overhead"}:
                if not self.video_duration:
                    continue
                measured_seconds = float(seconds)
                if unit == "per_source_second_after_overhead":
                    measured_seconds = max(
                        0.0,
                        measured_seconds - PHASE_FIXED_OVERHEAD.get(phase, 0.0),
                    )
                value = measured_seconds / self.video_duration
            elif unit == "per_output_second":
                if not self.output_seconds:
                    continue
                value = seconds / self.output_seconds
            else:
                value = seconds
            samples.setdefault(phase, {}).setdefault(unit, []).append(value)
        if not samples:
            return
        try:
            self.job_stats = _append_job_stat_samples(samples)
        except Exception as e:
            print(f"⚠️ Could not persist phase stats: {e!r}", file=sys.stderr)

    def _mark_work_activity(self, now=None):
        now = float(now or time.time())
        with self._state_lock:
            self.last_work_activity_at = now
            if self.operation_timeout_seconds:
                self.operation_deadline_at = now + self.operation_timeout_seconds

    def begin_operation(self, name: str, *, timeout_seconds: float,
                        expected_seconds: Optional[float] = None,
                        message: Optional[str] = None, **extra):
        """Declare a blocking operation so keepalives cannot hide its freeze."""
        now = time.time()
        timeout_seconds = max(1.0, float(timeout_seconds))
        with self._state_lock:
            self.operation_name = str(name)
            self.operation_timeout_seconds = timeout_seconds
            self.operation_deadline_at = now + timeout_seconds
            self.operation_expected_end_at = (
                now + max(0.0, float(expected_seconds))
                if expected_seconds is not None else None
            )
            self.last_work_activity_at = now
        self.heartbeat(message or f"Starting {name}.", force=True, **extra)

    def finish_operation(self, *, message: Optional[str] = None, **extra):
        with self._state_lock:
            previous_name = self.operation_name
            self.operation_name = None
            self.operation_timeout_seconds = None
            self.operation_deadline_at = None
            self.operation_expected_end_at = None
            self.last_work_activity_at = time.time()
        self.heartbeat(
            message or (f"Finished {previous_name}." if previous_name else "Blocking operation finished."),
            force=True,
            **extra,
        )

    @contextmanager
    def operation(self, name: str, *, timeout_seconds: float,
                  expected_seconds: Optional[float] = None,
                  message: Optional[str] = None, **extra):
        # Blocking operations may contain smaller independently bounded work.
        # Preserve the parent so completing a child both restores its watchdog
        # and counts as real progress for the parent's activity deadline.
        with self._state_lock:
            parent_operation = (
                self.operation_name,
                self.operation_timeout_seconds,
                self.operation_expected_end_at,
            )
        self.begin_operation(
            name,
            timeout_seconds=timeout_seconds,
            expected_seconds=expected_seconds,
            message=message,
            **extra,
        )
        try:
            yield
        finally:
            parent_name, parent_timeout_seconds, parent_expected_end_at = parent_operation
            if parent_name is None or parent_timeout_seconds is None:
                self.finish_operation(**extra)
            else:
                now = time.time()
                with self._state_lock:
                    finished_name = self.operation_name
                    self.operation_name = parent_name
                    self.operation_timeout_seconds = parent_timeout_seconds
                    self.operation_deadline_at = now + parent_timeout_seconds
                    self.operation_expected_end_at = parent_expected_end_at
                    self.last_work_activity_at = now
                self.heartbeat(
                    f"Finished {finished_name}; resumed {parent_name}.",
                    force=True,
                    **extra,
                )

    def emit(self, event_type: str, message: Optional[str] = None, **extra):
        if event_type in {"phase", "progress", "resume", "artifact"}:
            self._mark_work_activity()
        if extra.get("video_duration_seconds"):
            self.video_duration = float(extra["video_duration_seconds"])
        with self._state_lock:
            operation_payload = {
                "work_activity_at": self.last_work_activity_at,
                "operation_name": self.operation_name,
                "operation_deadline_at": self.operation_deadline_at,
            }
        payload = {
            "type": event_type,
            "timestamp": time.time(),
            "job_id": self.job_id,
            "phase": extra.pop("phase", self.phase),
            "phase_label": extra.pop("phase_label", self.phase_label),
            "phase_progress_percent": extra.pop("phase_progress_percent", self.phase_progress_percent),
            "progress_percent": extra.pop("progress_percent", self.progress_percent),
            **operation_payload,
        }
        phase_eta_seconds = extra.pop("phase_eta_seconds", None)
        # Backward-compatible input for callers that supplied yt-dlp's live
        # ETA before phase_eta_seconds existed.
        if phase_eta_seconds is None:
            phase_eta_seconds = extra.pop("eta_seconds", None)
        else:
            extra.pop("eta_seconds", None)
        caller_eta_is_estimated = bool(extra.pop("eta_is_estimated", False))
        # A caller-supplied ETA is normally measured (for example yt-dlp's
        # byte-rate ETA), unless it explicitly includes a prior such as muxing.
        measured = phase_eta_seconds is not None and not caller_eta_is_estimated
        if event_type == "summary" and extra.get("status") == "completed":
            phase_eta_seconds = 0
            total_eta_seconds = 0
            eta_state = "done"
        else:
            if phase_eta_seconds is None:
                estimate = self._estimate_phase_remaining()
                phase_eta_seconds = None if estimate is None else max(0, int(round(estimate)))
                # Past the halfway mark the blend is dominated by this job's own
                # measurement, so the value stops being a guess.
                measured = self.phase_progress_percent >= 50.0
            total_estimate = self._estimate_total_remaining(phase_eta_seconds)
            total_eta_seconds = None if total_estimate is None else max(0, int(round(total_estimate)))
            if phase_eta_seconds is None:
                eta_state = "calculating"
            else:
                eta_state = "live" if measured else "estimated"
        payload["phase_eta_seconds"] = phase_eta_seconds
        payload["total_eta_seconds"] = total_eta_seconds
        payload["eta_state"] = eta_state
        # Keep the legacy field populated for older clients.  New clients use
        # phase_eta_seconds and its explicit phase-only label.
        payload["eta_seconds"] = phase_eta_seconds
        if message:
            payload["message"] = message
        payload.update(extra)
        # Leading newline: yt-dlp writes \r-progress into the same stdout, and
        # an event glued behind such a fragment would not be recognized by the
        # server — its heartbeat would be lost and the stall monitor could
        # kill a healthy download.
        print(f"\n{EVENT_PREFIX}{json.dumps(payload, ensure_ascii=False)}", flush=True)

    def set_phase(self, phase: str, label: str, *, message: Optional[str] = None, phase_progress_percent: float = 0.0, **extra):
        # Record the finished phase for the exact completion breakdown.
        if self.phase in PHASE_ETA_ORDER:
            duration = time.time() - self.phase_started_at
            self.phase_durations[self.phase] = duration
        self.phase = phase
        self.phase_label = label
        self.phase_started_at = time.time()
        with self._state_lock:
            self.operation_name = None
            self.operation_timeout_seconds = None
            self.operation_deadline_at = None
            self.operation_expected_end_at = None
        self.phase_progress_percent = phase_progress_percent
        self.progress_percent = self._overall_progress(phase=phase, phase_progress_percent=phase_progress_percent)
        self.emit(
            "phase",
            message or label,
            phase=phase,
            phase_label=label,
            phase_progress_percent=phase_progress_percent,
            progress_percent=self.progress_percent,
            phase_durations_seconds={
                key: round(value, 3) for key, value in self.phase_durations.items()
            },
            **extra,
        )

    def progress(self, phase_progress_percent: float, *, message: Optional[str] = None, important: bool = False, **extra):
        self.phase_progress_percent = max(0.0, min(100.0, float(phase_progress_percent)))
        self.progress_percent = self._overall_progress(phase_progress_percent=self.phase_progress_percent)
        self.emit(
            "progress",
            message,
            phase_progress_percent=self.phase_progress_percent,
            progress_percent=self.progress_percent,
            important=important,
            **extra,
        )

    def heartbeat(self, message: Optional[str] = None, *, force: bool = False,
                  counts_as_work: bool = False, **extra):
        # message stays positional-friendly on purpose: a keyword-only signature
        # here silently broke the keepalive thread for months (TypeError eaten by
        # a bare except), and every long blocking step was then flagged as stalled.
        now = time.time()
        if counts_as_work:
            # Byte-level progress (e.g. an unknown-size download) is real work
            # even though it cannot be expressed as a percentage. Refresh the
            # operation deadline before the throttle check so the next emitted
            # event always carries the extended deadline. Plain keepalives must
            # never pass this flag — a genuinely frozen operation still expires.
            self._mark_work_activity(now)
        with self._state_lock:
            if not force and now - self.last_heartbeat_at < HEARTBEAT_INTERVAL_SECONDS:
                return
            self.last_heartbeat_at = now
        self.emit("heartbeat", message, **extra)

    def warning(self, message: str, **extra):
        self.emit("warning", message, important=True, **extra)

    def error(self, message: str, *, resumable: bool = True, **extra):
        self.emit("error", message, important=True, resumable=resumable, **extra)

    def artifact(self, kind: str, path: str, *, message: Optional[str] = None, important: bool = True, **extra):
        self.emit(
            "artifact",
            message or f"Saved {kind} to {path}",
            important=important,
            artifact={"kind": kind, "path": path},
            **extra,
        )

    def summary(self, status: str, message: str, *, resumable: bool = False, **extra):
        if status == "completed":
            # Close the final phase so the server can persist an exact phase
            # breakdown alongside its independently validated wall-clock end.
            if self.phase in PHASE_ETA_ORDER:
                self.phase_durations[self.phase] = time.time() - self.phase_started_at
            self._record_job_stats()
        phase = "completed" if status == "completed" else self.phase
        progress_percent = 100.0 if status == "completed" else self.progress_percent
        self.emit(
            "summary",
            message,
            status=status,
            phase=phase,
            phase_label=self.phase_label if status != "completed" else "Completed",
            progress_percent=progress_percent,
            phase_progress_percent=100.0 if status == "completed" else self.phase_progress_percent,
            resumable=resumable,
            worker_duration_seconds=round(time.time() - self.started_at, 3),
            phase_durations_seconds={
                key: round(value, 3) for key, value in self.phase_durations.items()
            },
            **extra,
        )


JOB_REPORTER = JobReporter()


def set_job_reporter(reporter: JobReporter):
    global JOB_REPORTER
    JOB_REPORTER = reporter


def _start_keepalive(interval_seconds=15):
    """Emit a heartbeat every few seconds regardless of pipeline progress.

    Whisper/FFmpeg/yt-dlp's merge can crunch for minutes without emitting an
    event; without this, the server's stall detector flags perfectly healthy
    jobs as stalled. With it, "stalled" means the process is truly frozen or
    asleep."""
    import threading

    def _beat():
        reported_failure = False
        while True:
            time.sleep(interval_seconds)
            try:
                JOB_REPORTER.heartbeat("Worker alive.")
            except Exception as e:
                # Never swallow this silently again: a broken keepalive looks
                # exactly like a frozen worker and gets the job killed.
                if not reported_failure:
                    reported_failure = True
                    print(f"⚠️ Keepalive heartbeat failed: {e!r}", file=sys.stderr, flush=True)

    threading.Thread(target=_beat, daemon=True, name="keepalive").start()


def _prevent_windows_sleep():
    """Keep Windows awake while a job runs.

    Lid-close / idle standby freezes the worker mid-transcription and the job
    looks hung for hours. The execution-state flag clears automatically when
    the process exits, so no cleanup is needed. The display may still turn off."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        print("🔒 Standby disabled while this job is running (display may still turn off).")
    except Exception as e:
        print(f"⚠️ Could not disable standby: {e}")

def _save_json_file(path, payload):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _load_json_file(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _save_text_file(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text or "")


def _extract_words_for_analysis(transcript_result):
    # Rounded to 2 decimals: full float precision only wastes prompt tokens.
    words = []
    for segment in transcript_result.get('segments', []):
        for word in segment.get('words', []):
            words.append({
                'w': word.get('word', ''),
                's': round(float(word.get('start', 0)), 2),
                'e': round(float(word.get('end', 0)), 2),
            })
    return words


def _build_analysis_input_payload(transcript_result, video_duration, source_url=None, input_video=None):
    return {
        "schema_version": 1,
        "source_url": source_url,
        "input_video": input_video,
        "video_duration": round(float(video_duration), 3),
        "transcript": transcript_result,
    }


def _save_json_checkpoint(output_dir, video_title, suffix, payload):
    path = os.path.join(output_dir, f"{video_title}_{suffix}.json")
    _save_json_file(path, payload)
    JOB_REPORTER.artifact(suffix, path)
    return path


def _save_text_checkpoint(output_dir, video_title, suffix, text):
    path = os.path.join(output_dir, f"{video_title}_{suffix}.txt")
    _save_text_file(path, text)
    JOB_REPORTER.artifact(suffix, path, important=False)
    return path


def _extract_text_for_range(transcript_result, start, end):
    parts = []
    for segment in transcript_result.get("segments", []):
        seg_start = float(segment.get("start", 0))
        seg_end = float(segment.get("end", 0))
        if seg_end <= start or seg_start >= end:
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _extract_words_for_range(words, start, end):
    extracted = []
    for word in words:
        word_start = float(word.get("s", 0))
        word_end = float(word.get("e", 0))
        if word_end <= start or word_start >= end:
            continue
        extracted.append(word)
    return extracted


def _build_transcript_windows(transcript_result, video_duration, window_seconds=GEMINI_WINDOW_SECONDS, overlap_seconds=GEMINI_WINDOW_OVERLAP_SECONDS):
    # Segment-aligned windowing lives in clip_selection so it stays unit-testable.
    return build_transcript_windows(transcript_result, video_duration, window_seconds, overlap_seconds)


def _iter_batches(items, batch_size):
    for index in range(0, len(items), batch_size):
        yield index // batch_size, items[index:index + batch_size]


def _merge_cost_analyses(cost_analyses):
    valid = [cost for cost in cost_analyses if cost]
    if not valid:
        return None
    return {
        "input_tokens": sum(cost.get("input_tokens", 0) for cost in valid),
        "output_tokens": sum(cost.get("output_tokens", 0) for cost in valid),
        "thinking_tokens": sum(cost.get("thinking_tokens", 0) for cost in valid),
        "input_cost": sum(cost.get("input_cost", 0.0) for cost in valid),
        "output_cost": sum(cost.get("output_cost", 0.0) for cost in valid),
        "total_cost": sum(cost.get("total_cost", 0.0) for cost in valid),
        "model": valid[-1].get("model", GEMINI_ANALYSIS_MODEL),
        "price_estimated": any(cost.get("price_estimated") for cost in valid),
    }


def _normalize_scored_windows(payload, video_duration):
    if not isinstance(payload, dict):
        raise ValueError("Scoring payload was not a JSON object.")
    windows = payload.get("windows")
    if not isinstance(windows, list):
        raise ValueError("Scoring payload did not contain a valid 'windows' array.")
    normalized = []
    for item in windows:
        if not isinstance(item, dict):
            continue
        try:
            start = round(max(0.0, float(item["start"])), 3)
            end = round(min(float(video_duration), float(item["end"])), 3)
            score = int(item.get("score", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        normalized.append({
            "id": str(item.get("id") or ""),
            "start": start,
            "end": end,
            "score": max(0, min(100, score)),
            "reason": str(item.get("reason") or "").strip(),
        })
    return normalized


class GeminiWorkerError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result if isinstance(result, dict) else {}
        self.error_type = str(self.result.get("error_type") or "worker_error")


def _call_gemini_worker(
    mode, payload, *, output_dir, video_title, strategy, batch_index,
    total_batches, attempt, timeout_seconds=GEMINI_REQUEST_TIMEOUT_SECONDS,
    artifact_suffix=None,
):
    request_timeout = max(1.0, float(timeout_seconds))
    request_name = (
        f"Gemini {mode} batch {batch_index + 1}/{total_batches} "
        f"attempt {attempt}"
    )
    with JOB_REPORTER.operation(
        request_name,
        timeout_seconds=request_timeout + GEMINI_REQUEST_WATCHDOG_GRACE_SECONDS,
        message=f"Starting {request_name}.",
        category="gemini",
        attempt=attempt,
        batch_index=batch_index + 1,
        total_batches=total_batches,
    ):
        return _run_gemini_worker(
            mode,
            payload,
            output_dir=output_dir,
            video_title=video_title,
            strategy=strategy,
            batch_index=batch_index,
            total_batches=total_batches,
            attempt=attempt,
            timeout_seconds=request_timeout,
            artifact_suffix=artifact_suffix,
        )


def _run_gemini_worker(
    mode, payload, *, output_dir, video_title, strategy, batch_index,
    total_batches, attempt, timeout_seconds, artifact_suffix=None,
):
    suffix = f"_{sanitize_filename(str(artifact_suffix))}" if artifact_suffix else ""
    artifact_stem = f"{video_title}_{mode}_batch_{batch_index + 1}{suffix}_attempt_{attempt}"
    request_path = os.path.join(output_dir, f"{artifact_stem}.request.json")
    response_path = os.path.join(output_dir, f"{artifact_stem}.response.json")
    if os.path.exists(response_path):
        os.remove(response_path)
    _save_json_file(request_path, payload)

    worker_cmd = [
        sys.executable,
        "-u",
        GEMINI_WORKER_SCRIPT,
        "--mode",
        mode,
        "--input",
        request_path,
        "--output",
        response_path,
        "--strategy",
        strategy,
        "--model",
        GEMINI_ANALYSIS_MODEL,
    ]

    process = subprocess.Popen(worker_cmd)
    start_time = time.time()
    slow_warning_emitted = False

    try:
        while process.poll() is None:
            elapsed = time.time() - start_time
            if not slow_warning_emitted and elapsed >= GEMINI_SLOW_WARNING_SECONDS:
                JOB_REPORTER.emit(
                    "slow",
                    f"Gemini request is taking longer than expected ({int(elapsed)}s).",
                    important=True,
                    category="gemini",
                    attempt=attempt,
                    batch_index=batch_index + 1,
                    total_batches=total_batches,
                )
                slow_warning_emitted = True
            JOB_REPORTER.heartbeat(
                message=f"Gemini {mode} batch {batch_index + 1}/{total_batches} is still running...",
                category="gemini",
                attempt=attempt,
                batch_index=batch_index + 1,
                total_batches=total_batches,
            )
            if elapsed >= timeout_seconds:
                raise TimeoutError(
                    f"Gemini {mode} batch {batch_index + 1}/{total_batches} exceeded the timeout of {timeout_seconds}s."
                )
            time.sleep(1)
    finally:
        # On timeout/exception, don't leave the Gemini worker subprocess orphaned.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    worker_result = None
    if os.path.exists(response_path):
        try:
            with open(response_path, "r", encoding="utf-8") as f:
                worker_result = json.load(f)
        except Exception:
            worker_result = None
    if process.returncode != 0:
        detail = (worker_result or {}).get("error")
        error_type = (worker_result or {}).get("error_type")
        reason = f" ({error_type}: {detail})" if error_type or detail else ""
        raise GeminiWorkerError(
            f"Gemini worker failed for {mode} batch {batch_index + 1}/{total_batches} "
            f"with exit code {process.returncode}{reason}.",
            worker_result,
        )
    if worker_result is None:
        raise GeminiWorkerError(
            f"Gemini worker did not produce a readable response file for {mode} "
            f"batch {batch_index + 1}/{total_batches}.",
        )
    return worker_result


def _strip_code_fences(text):
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_json_candidate(text):
    cleaned = _strip_code_fences(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start:end + 1]
    return cleaned


def _escape_invalid_unicode_escapes(text):
    chars = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "u":
            hex_digits = text[i + 2:i + 6]
            if len(hex_digits) < 4 or any(ch not in "0123456789abcdefABCDEF" for ch in hex_digits):
                chars.append("\\\\u")
                i += 2
                continue
        chars.append(text[i])
        i += 1
    return "".join(chars)


def _parse_json_response_text(text):
    if not text:
        raise ValueError("Gemini returned an empty response body.")

    candidate = _extract_json_candidate(text).replace("\x00", "").strip()
    if not candidate:
        raise ValueError("Gemini response did not contain a JSON object.")

    parse_attempts = [candidate]
    sanitized_candidate = _escape_invalid_unicode_escapes(candidate)
    if sanitized_candidate != candidate:
        parse_attempts.append(sanitized_candidate)

    last_error = None
    for parse_candidate in parse_attempts:
        try:
            return json.loads(parse_candidate)
        except json.JSONDecodeError as e:
            last_error = e

    raise ValueError(f"Failed to parse Gemini JSON response: {last_error}")


def _get_response_text(response):
    try:
        text = response.text
        if text:
            return text
    except Exception:
        pass

    parts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(part_text)

    return "\n".join(parts).strip()


def _calculate_cost_analysis(response, model_name):
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None

    input_price_per_million = 0.10
    output_price_per_million = 0.40

    prompt_tokens = usage.prompt_token_count
    output_tokens = usage.candidates_token_count

    input_cost = (prompt_tokens / 1_000_000) * input_price_per_million
    output_cost = (output_tokens / 1_000_000) * output_price_per_million
    total_cost = input_cost + output_cost

    return {
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
        "model": model_name,
    }


def _selection_limits(video_duration):
    """Keep normal jobs unchanged; inspect a wider pool only for 2h+ sources."""
    return selection_limits(
        video_duration,
        normal_shortlist=GEMINI_SHORTLIST_LIMIT,
        long_shortlist=GEMINI_LONG_SHORTLIST_LIMIT,
        long_video_seconds=GEMINI_LONG_VIDEO_SECONDS,
        max_clips=GEMINI_MAX_CLIPS,
    )


def _normalize_shorts_payload(payload, video_duration, words=None, max_clips=None):
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()

    if not isinstance(payload, dict):
        raise ValueError("Gemini response was not a JSON object.")

    shorts = payload.get("shorts")
    if not isinstance(shorts, list):
        raise ValueError("Gemini response did not contain a valid 'shorts' array.")

    normalized_shorts = []
    seen_ranges = set()
    max_end = round(float(video_duration), 3)

    for clip in shorts:
        if not isinstance(clip, dict):
            continue

        try:
            start = round(max(0.0, float(clip["start"])), 3)
            end = round(min(max_end, float(clip["end"])), 3)
        except (KeyError, TypeError, ValueError):
            continue

        if end <= start:
            continue

        duration = round(end - start, 3)
        if duration > 60.0:
            end = round(min(max_end, start + 60.0), 3)
            duration = round(end - start, 3)

        if duration < 15.0:
            extended_end = round(min(max_end, start + 15.0), 3)
            if round(extended_end - start, 3) >= 15.0:
                end = extended_end
                duration = round(end - start, 3)

        if duration < 15.0 or duration > 60.0:
            continue

        # Snap onto word boundaries + surrounding silence: the LLM's second
        # guesses are approximate, the Whisper timestamps are ground truth.
        if words:
            start, end = snap_clip_to_words(start, end, words, max_end)
            duration = round(end - start, 3)

        clip_key = (start, end)
        if clip_key in seen_ranges:
            continue
        seen_ranges.add(clip_key)

        normalized_shorts.append({
            "start": start,
            "end": end,
            "video_description_for_tiktok": str(clip.get("video_description_for_tiktok") or "").strip() or "AI-selected highlight clip.",
            "video_description_for_instagram": str(clip.get("video_description_for_instagram") or "").strip() or "AI-selected highlight clip.",
            "video_title_for_youtube_short": str(clip.get("video_title_for_youtube_short") or "").strip()[:100] or "AI-selected highlight",
            "viral_hook_text": str(clip.get("viral_hook_text") or "").strip()[:120] or "Watch this",
            "source_window_id": str(clip.get("source_window_id") or "").strip() or None,
            "predicted_score": int(clip.get("predicted_score", 0) or 0),
        })

    if not normalized_shorts:
        raise ValueError("Gemini did not return any valid clips after validation.")

    clip_limit = max(1, int(max_clips if max_clips is not None else GEMINI_MAX_CLIPS))
    return {"shorts": choose_distinct_clips(normalized_shorts, max_clips=clip_limit)}


def _build_fallback_metadata(video_title, transcript, duration, output_filename, analysis_error, attempts, cost_analysis=None):
    title = video_title.replace("_", " ").strip() or "Fallback video"

    metadata = {
        "schema_version": 1,
        "processing_mode": "full_video_fallback",
        "analysis_status": "fallback",
        "analysis_error": analysis_error,
        "analysis_attempts": attempts,
        "transcript": transcript,
        "shorts": [
            {
                "start": 0.0,
                "end": round(float(duration), 3),
                "video_description_for_tiktok": "Automatic full-video fallback because AI clip detection failed.",
                "video_description_for_instagram": "Automatic full-video fallback because AI clip detection failed.",
                "video_title_for_youtube_short": title[:100],
                "viral_hook_text": "Automatic fallback",
                "output_filename": os.path.basename(output_filename),
            }
        ],
    }

    if cost_analysis:
        metadata["cost_analysis"] = cost_analysis

    return metadata

# Load the YOLO model once (Keep for backup or scene analysis if needed)
model = YOLO('yolov8n.pt')

# --- MediaPipe Setup (Tasks API for mediapipe >= 0.10.21) ---
# Auto-download the face detection model if not present
_FACE_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'blaze_face_short_range.tflite')
if not os.path.exists(_FACE_MODEL_PATH):
    print("📥 Downloading MediaPipe face detection model...")
    import urllib.request
    urllib.request.urlretrieve(
        'https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite',
        _FACE_MODEL_PATH
    )
    print("✅ Model downloaded.")

_face_detector_options = mp_vision.FaceDetectorOptions(
    base_options=mp_python.BaseOptions(model_asset_path=_FACE_MODEL_PATH),
    running_mode=mp_vision.RunningMode.IMAGE,
    min_detection_confidence=0.5
)
face_detector = mp_vision.FaceDetector.create_from_options(_face_detector_options)

class SmoothedCameraman:
    """
    Handles smooth camera movement.
    Simplified Logic: "Heavy Tripod"
    Only moves if the subject leaves the center safe zone.
    Moves slowly and linearly.
    """
    def __init__(self, output_width, output_height, video_width, video_height, aspect_ratio=ASPECT_RATIO):
        self.output_width = output_width
        self.output_height = output_height
        self.video_width = video_width
        self.video_height = video_height

        # Initial State
        self.current_center_x = video_width / 2
        self.target_center_x = video_width / 2
        self.current_center_y = video_height / 2
        self.target_center_y = video_height / 2

        # Calculate a crop that fits on BOTH axes. The previous implementation
        # always returned the full source height even when a narrow portrait
        # source required a shorter crop, which stretched portrait/square video.
        self.crop_width, self.crop_height = fit_even_output_dimensions(
            video_width, video_height, aspect_ratio,
        )
             
        # Safe Zone: 20% of the video width
        # As long as the target is within this zone relative to current center, DO NOT MOVE.
        self.safe_zone_radius = self.crop_width * 0.25
        self.safe_zone_radius_y = self.crop_height * 0.25

    def reset(self):
        """Return to a neutral crop before acquiring a target in a new shot."""
        self.current_center_x = self.video_width / 2
        self.target_center_x = self.video_width / 2
        self.current_center_y = self.video_height / 2
        self.target_center_y = self.video_height / 2

    def update_target(self, face_box):
        """
        Updates the target center based on detected face/person.
        """
        if face_box:
            x, y, w, h = face_box
            self.target_center_x = x + w / 2
            self.target_center_y = y + h / 2

    @staticmethod
    def _advance_axis(current, target, crop_span, safe_radius):
        diff = target - current
        if abs(diff) > crop_span * 0.6:
            return target
        if abs(diff) > safe_radius:
            direction = 1 if diff > 0 else -1
            next_value = current + direction * 3.0
            if (direction > 0 and next_value > target) or (direction < 0 and next_value < target):
                return target
            return next_value
        return current
    
    def get_crop_box(self, force_snap=False):
        """
        Returns the (x1, y1, x2, y2) for the current frame.
        """
        if force_snap:
            self.current_center_x = self.target_center_x
            self.current_center_y = self.target_center_y
        else:
            self.current_center_x = self._advance_axis(
                self.current_center_x, self.target_center_x,
                self.crop_width, self.safe_zone_radius,
            )
            self.current_center_y = self._advance_axis(
                self.current_center_y, self.target_center_y,
                self.crop_height, self.safe_zone_radius_y,
            )
                
        # Clamp center
        half_crop = self.crop_width / 2
        
        if self.current_center_x - half_crop < 0:
            self.current_center_x = half_crop
        if self.current_center_x + half_crop > self.video_width:
            self.current_center_x = self.video_width - half_crop

        half_crop_y = self.crop_height / 2
        if self.current_center_y - half_crop_y < 0:
            self.current_center_y = half_crop_y
        if self.current_center_y + half_crop_y > self.video_height:
            self.current_center_y = self.video_height - half_crop_y
            
        x1 = int(self.current_center_x - half_crop)
        x2 = int(self.current_center_x + half_crop)
        
        x1 = max(0, x1)
        x2 = min(self.video_width, x2)
        
        y1 = max(0, int(self.current_center_y - half_crop_y))
        y2 = min(self.video_height, y1 + self.crop_height)
        y1 = max(0, y2 - self.crop_height)
        
        return x1, y1, x2, y2

def detect_face_candidates(frame):
    """
    Returns list of all detected faces using MediaPipe Tasks FaceDetector.
    """
    height, width, _ = frame.shape
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
    results = face_detector.detect(mp_image)
    
    candidates = []
    
    if not results.detections:
        return []
        
    for detection in results.detections:
        bbox = detection.bounding_box
        x = bbox.origin_x
        y = bbox.origin_y
        w = bbox.width
        h = bbox.height
        
        candidates.append({
            'box': [x, y, w, h],
            'score': w * h  # Area as score
        })
            
    return candidates

def detect_person_boxes(frame):
    """
    All YOLO person boxes as [x, y, w, h]. Full-body detection stays reliable
    on wide shots where the short-range face model misses distant or profile
    faces — this is the robust people-count signal for the layout decision.
    """
    results = model(frame, verbose=False, classes=[0])
    boxes = []
    for result in results or []:
        for box in result.boxes:
            try:
                conf = float(box.conf[0])
            except (TypeError, IndexError):
                conf = 1.0
            if conf < 0.5:
                continue
            x1, y1, x2, y2 = [int(i) for i in box.xyxy[0]]
            if x2 > x1 and y2 > y1:
                boxes.append([x1, y1, x2 - x1, y2 - y1])
    return boxes


def detect_person_yolo(frame):
    """
    Fallback: Detect largest person using YOLO when face detection fails.
    Returns [x, y, w, h] of the person's 'upper body' approximation.
    """
    # Use the globally loaded model
    results = model(frame, verbose=False, classes=[0]) # class 0 is person
    
    if not results:
        return None
        
    best_box = None
    max_area = 0
    
    for result in results:
        boxes = result.boxes
        for box in boxes:
            try:
                confidence = float(box.conf[0])
            except (TypeError, IndexError):
                confidence = 1.0
            if confidence < 0.5:
                continue
            x1, y1, x2, y2 = [int(i) for i in box.xyxy[0]]
            w = x2 - x1
            h = y2 - y1
            area = w * h
            
            if area > max_area:
                max_area = area
                # Focus on the top 40% of the person (head/chest) for framing
                # This approximates where the face is if we can't detect it directly
                face_h = int(h * 0.4)
                best_box = [x1, y1, w, face_h]
                
    return best_box

def create_general_frame(frame, output_width, output_height):
    """
    Creates a 'General Shot' frame: 
    - Background: Blurred zoom of original
    - Foreground: Original video scaled to fit width, centered vertically.
    """
    orig_h, orig_w = frame.shape[:2]
    
    # 1. Background: generic "cover" scaling works for landscape, portrait
    # and square sources without negative slices or distortion.
    bg_scale = max(output_width / orig_w, output_height / orig_h)
    bg_w = max(output_width, int(round(orig_w * bg_scale)))
    bg_h = max(output_height, int(round(orig_h * bg_scale)))
    bg_resized = cv2.resize(frame, (bg_w, bg_h))
    start_x = max(0, (bg_w - output_width) // 2)
    start_y = max(0, (bg_h - output_height) // 2)
    background = bg_resized[start_y:start_y + output_height,
                            start_x:start_x + output_width]
        
    # Blur background
    background = cv2.GaussianBlur(background, (51, 51), 0)
    
    # 2. Foreground: generic "contain" scaling preserves the whole source.
    scale = min(output_width / orig_w, output_height / orig_h)
    fg_w = max(1, min(output_width, int(round(orig_w * scale))))
    fg_h = max(1, min(output_height, int(round(orig_h * scale))))
    foreground = cv2.resize(frame, (fg_w, fg_h))
    
    # 3. Overlay
    y_offset = (output_height - fg_h) // 2
    x_offset = (output_width - fg_w) // 2
    
    # Clone background to avoid modifying it
    final_frame = background.copy()
    final_frame[y_offset:y_offset + fg_h, x_offset:x_offset + fg_w] = foreground
    
    return final_frame


def create_split_frame(frame, output_width, output_height, centers, stacked=True):
    """
    Opus-Clip style split layout for two-person shots: crop a window around
    each face and stack them (vertically for 9:16, side by side for 1:1).
    Both people stay large in frame — no blurred bars, nobody cropped out.
    """
    src_h, src_w = frame.shape[:2]
    windows = split_crop_windows(src_w, src_h, output_width, output_height, centers, stacked=stacked)

    if stacked:
        panel_w, panel_h = output_width, max(1, output_height // 2)
    else:
        panel_w, panel_h = max(1, output_width // 2), output_height

    panels = []
    for (x1, y1, x2, y2) in windows:
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            crop = frame
        panels.append(cv2.resize(crop, (panel_w, panel_h)))

    combined = cv2.vconcat(panels) if stacked else cv2.hconcat(panels)
    if combined.shape[0] != output_height or combined.shape[1] != output_width:
        # Odd output sizes leave a 1px remainder after halving — snap to size.
        combined = cv2.resize(combined, (output_width, output_height))
    return combined


_WATERMARK_FONT_CANDIDATES = [
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\segoeuib.ttf",
    "/mnt/c/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def _find_default_watermark_image():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(script_dir, "dashboard", "public", "logo-openshorts.png")
    return candidate if os.path.exists(candidate) else None


def _render_watermark_rgba(output_width):
    """Render the watermark as a PIL RGBA image, opacity already baked into
    the alpha channel. WATERMARK_TEXT wins; otherwise the configured/default
    logo PNG; otherwise a plain text fallback."""
    from PIL import Image as PILImage, ImageDraw, ImageFont

    wm_width = max(2, int(output_width * WATERMARK_WIDTH_FRACTION))
    image_path = WATERMARK_IMAGE or _find_default_watermark_image()
    if image_path and not os.path.isabs(image_path) and not os.path.exists(image_path):
        # Resolve relative WATERMARK_IMAGE paths against the project folder so
        # the .env entry works regardless of the worker's working directory.
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), image_path)
        if os.path.exists(candidate):
            image_path = candidate

    if not WATERMARK_TEXT and image_path:
        img = PILImage.open(image_path).convert("RGBA")
        scale = wm_width / max(img.width, 1)
        img = img.resize((wm_width, max(1, int(img.height * scale))), PILImage.LANCZOS)
    else:
        text = WATERMARK_TEXT or "OpenShorts"
        font = None
        for path in _WATERMARK_FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, 120)
                    break
                except Exception:
                    continue
        if font is None:
            font = ImageFont.load_default()
        probe = PILImage.new("RGBA", (4, 4))
        draw = ImageDraw.Draw(probe)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = max(bbox[2] - bbox[0], 1)
        text_h = max(bbox[3] - bbox[1], 1)
        img = PILImage.new("RGBA", (text_w + 8, text_h + 8), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=(255, 255, 255, 255))
        scale = wm_width / img.width
        img = img.resize((wm_width, max(1, int(img.height * scale))), PILImage.LANCZOS)

    # Bake the global opacity into the alpha channel so every consumer
    # (frame blender, FFmpeg overlay) renders it equally subtle.
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * WATERMARK_OPACITY))
    img.putalpha(a)
    return img


def _build_watermark_blender(output_width, output_height):
    """Precompute the centered watermark blend for the OpenCV frame loop.
    Returns (premultiplied_bgr, inverse_alpha, x, y, w, h) or None."""
    if not WATERMARK_ENABLED:
        return None
    try:
        rgba = _render_watermark_rgba(output_width)
    except Exception as e:
        print(f"⚠️ Watermark disabled (render failed): {e}")
        return None
    arr = np.array(rgba, dtype=np.float32)
    h, w = arr.shape[:2]
    x = (output_width - w) // 2
    y = (output_height - h) // 2
    if x < 0 or y < 0 or h < 1 or w < 1:
        return None
    alpha = arr[:, :, 3:4] / 255.0
    bgr = arr[:, :, [2, 1, 0]]
    return (bgr * alpha, 1.0 - alpha, x, y, w, h)


def _apply_watermark(frame, blender):
    premul, inv_alpha, x, y, w, h = blender
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    frame[y:y + h, x:x + w] = (roi * inv_alpha + premul).astype(np.uint8)
    return frame


def analyze_scenes_strategy(video_path, scenes, layout_style="smart"):
    """
    Analyzes each scene to pick a layout: TRACK (zoom on one person),
    SPLIT (two people stacked) or GENERAL (blurred wide shot).
    Returns one evidence-backed SceneLayoutDecision per source shot.
    """
    if layout_style == "wide":
        return [
            SceneLayoutDecision("GENERAL", None, 1.0, "wide layout requested")
            for _ in scenes
        ]

    cap = cv2.VideoCapture(video_path)
    decisions = []

    if not cap.isOpened():
        return []

    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or 1920
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0

    for start, end in tqdm(scenes, desc="   Analyzing Scenes"):
        # Duration-aware coverage: short shots still get five observations;
        # long shots get up to 24 instead of being judged from five snapshots.
        frames_to_check = sample_scene_frames(
            start.get_frames(),
            end.get_frames(),
            fps,
        )

        face_samples = []
        person_samples = []
        for f_idx in frames_to_check:
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ret, frame = cap.read()
            if not ret: continue

            candidates = detect_face_candidates(frame)
            face_samples.append([c['box'] for c in candidates])
            if layout_style != "zoom":
                person_samples.append(detect_person_boxes(frame))

        decision = decide_scene_layout_detailed(
            face_samples, frame_width, layout_style=layout_style,
            person_samples=person_samples,
        )
        decisions.append(decision)

    cap.release()
    return decisions

def detect_scenes(video_path):
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector())
    if open_video is not None:
        video = open_video(video_path)
        scene_manager.detect_scenes(video=video)
        scene_list = scene_manager.get_scene_list()
        fps = video.frame_rate
    else:
        video_manager = VideoManager([video_path])
        video_manager.set_downscale_factor()
        video_manager.start()
        scene_manager.detect_scenes(frame_source=video_manager)
        scene_list = scene_manager.get_scene_list()
        fps = video_manager.get_framerate()
        video_manager.release()
    return scene_list, fps

def get_video_resolution(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"Could not open video file {video_path}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return width, height


def sanitize_filename(filename):
    """Remove invalid characters from filename."""
    # '#' breaks clip URLs: the browser parses it as a fragment, so files
    # named after hashtag-titles could never be requested by the frontend.
    filename = re.sub(r'[<>:"/\\|?*#]', '', filename)
    filename = filename.replace(' ', '_')
    return filename[:100]


def _make_ytdlp_progress_hooks():
    """Create per-download yt-dlp hooks with isolated throttling state."""
    progress_state = {"last_emit": 0.0, "last_bucket": -1, "downloaded_by_file": {}}

    def download_progress_hook(data):
        if data.get("status") != "downloading":
            return
        total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        if not total_bytes:
            # Unknown total size: a percentage is impossible, but received
            # bytes are real work. Without marking them the download
            # operation's freeze deadline would kill a healthy long download
            # after BLOCKING_OPERATION_STALL_SECONDS even though data flows.
            # bestvideo+bestaudio arrives as separate files whose byte
            # counters restart at zero, so movement is tracked per file — a
            # shared counter would read the second stream as "no new bytes"
            # until it outgrew the first one. The dict stays tiny: one entry
            # per downloaded file of this job.
            file_key = data.get("filename") or data.get("tmpfilename") or ""
            seen_bytes = progress_state["downloaded_by_file"]
            if downloaded > seen_bytes.get(file_key, 0):
                seen_bytes[file_key] = downloaded
                JOB_REPORTER.heartbeat(
                    f"Downloading video... {downloaded / 1_000_000:.1f} MB "
                    "(total size unknown)",
                    counts_as_work=True,
                    category="download",
                )
            else:
                JOB_REPORTER.heartbeat(message="Downloading video...", category="download")
            return
        percent = max(0.0, min(100.0, (downloaded / total_bytes) * 100.0))
        now = time.time()
        bucket = int(percent // 5)
        if now - progress_state["last_emit"] < 1.0 and bucket == progress_state["last_bucket"]:
            return
        progress_state["last_emit"] = now
        progress_state["last_bucket"] = bucket
        yt_dlp_eta = data.get("eta")
        phase_eta = None
        if yt_dlp_eta is not None:
            phase_eta = float(yt_dlp_eta) + _estimated_merge_seconds(JOB_REPORTER.video_duration)
        JOB_REPORTER.progress(
            percent * 0.98,
            message=f"Downloading video... {percent:.1f}%",
            important=bucket % 2 == 0,
            eta_seconds=phase_eta,
            eta_is_estimated=phase_eta is not None,
            category="download",
        )

    def download_postprocessor_hook(data):
        # yt-dlp muxes the separate video/audio streams with FFmpeg after the
        # download. This operation has its own ETA and freeze deadline because
        # yt-dlp does not publish byte progress while FFmpegMerger is running.
        name = data.get("postprocessor") or "post-processing"
        status = data.get("status")
        if status == "started":
            label = "Merging video and audio..." if "Merger" in name else f"Post-processing ({name})..."
            expected_seconds = _estimated_merge_seconds(JOB_REPORTER.video_duration)
            timeout_seconds = max(
                MERGE_MIN_STALL_SECONDS,
                int(math.ceil(expected_seconds * MERGE_STALL_MULTIPLIER)),
            )
            JOB_REPORTER.begin_operation(
                f"yt-dlp:{name}",
                timeout_seconds=timeout_seconds,
                expected_seconds=expected_seconds,
                message=label,
                category="download",
            )
            # 100% means the entire download phase is done. Keep one final
            # percent for muxing and publish its own non-zero ETA.
            JOB_REPORTER.progress(
                99.0,
                message=label,
                important=True,
                phase_eta_seconds=int(round(expected_seconds)),
                eta_is_estimated=True,
                category="download",
            )
        elif status == "finished":
            JOB_REPORTER.finish_operation(
                message=f"Post-processing ({name}) finished.",
                category="download",
            )
        else:
            JOB_REPORTER.heartbeat(f"Post-processing ({name}): {status}", category="download")

    return download_progress_hook, download_postprocessor_hook


_YOUTUBE_REFUSAL_MARKERS = (
    "403", "forbidden", "429", "too many requests",
    "unable to download video data", "fragment not found",
)


def _is_youtube_refusal(error):
    """True when YouTube blocked the transfer itself, rather than something on
    our side failing. Only these are worth retrying with a different format or
    session — a full disk or a dead URL would fail the same way every time."""
    text = str(error).lower()
    return any(marker in text for marker in _YOUTUBE_REFUSAL_MARKERS)


def download_youtube_video(url, output_dir=".", resume=False):
    """
    Downloads a YouTube video using yt-dlp.
    Returns the path to the downloaded video and the video title.

    With resume=True already finished format files and .part fragments are kept
    so a re-download after an interrupted job continues instead of starting over.
    """
    print(f"🔍 Debug: yt-dlp version: {yt_dlp.version.__version__}")
    print("📥 Downloading video from YouTube...")
    step_start_time = time.time()
    download_progress_hook, download_postprocessor_hook = _make_ytdlp_progress_hooks()

    # Look for cookies in the project directory (multiple common filenames)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _cookie_candidates = [
        os.path.join(_script_dir, 'www.youtube.com_cookies.txt'),
        os.path.join(_script_dir, 'cookies.txt'),
        '/app/cookies.txt',  # Docker fallback
    ]
    cookies_path = None
    for _cp in _cookie_candidates:
        if os.path.exists(_cp):
            cookies_path = _cp
            print(f"🍪 Found cookies file: {cookies_path}")
            break

    cookies_env = os.environ.get("YOUTUBE_COOKIES")
    if cookies_env:
        print("🍪 Found YOUTUBE_COOKIES env var, writing cookies file...")
        if not cookies_path:
            cookies_path = os.path.join(_script_dir, 'cookies.txt')
        try:
            with open(cookies_path, 'w') as f:
                f.write(cookies_env)
            if os.path.exists(cookies_path):
                 print(f"   Debug: Cookies file created. Size: {os.path.getsize(cookies_path)} bytes")
                 with open(cookies_path, 'r') as f:
                     content = f.read(100)
                     print(f"   Debug: First 100 chars of cookie file: {content}")
        except Exception as e:
            print(f"⚠️ Failed to write cookies file: {e}")
            cookies_path = None
    else:
        if not cookies_path:
            print("⚠️ No cookies file found and YOUTUBE_COOKIES env var not set.")
    
    js_runtimes = None
    if shutil.which('deno'):
        js_runtimes = {'deno': {}}
        print("🧠 Using Deno for yt-dlp JS challenges")
    elif shutil.which('node'):
        js_runtimes = {'node': {}}
        print("🧠 Using Node.js for yt-dlp JS challenges")
    else:
        print("⚠️ No supported JS runtime found for yt-dlp (Deno/Node). Download quality may be limited.")

    # yt-dlp options: adapt based on whether cookies are available
    # WITH cookies: use 'web' client, DO NOT skip webpage (needed for cookie auth)
    # WITHOUT cookies: use more reliable non-auth clients and skip webpage

    # Do NOT override player_client: yt-dlp's curated defaults (tv_downgraded/
    # web_safari with cookies, android_vr/web_safari anonymous) are exactly the
    # clients that still serve HD without PO tokens. Our old hardcoded list
    # (web_safari/ios/web/mweb/android) hit PO-token/SABR walls and silently
    # degraded downloads to the token-free 360p format.
    _extractor_args = {}
    if cookies_path:
        print("🔧 Using cookie-authenticated yt-dlp config (default clients)")
    else:
        print("🔧 Using anonymous yt-dlp config (default clients)")

    _COMMON_YDL_OPTS = {
        'quiet': False,
        'verbose': True,
        'no_warnings': False,
        'cookiefile': cookies_path if cookies_path else None,
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'nocheckcertificate': True,
        'cachedir': False,
        'extractor_args': _extractor_args,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
        },
        'progress_hooks': [download_progress_hook],
        'postprocessor_hooks': [download_postprocessor_hook],
    }
    if js_runtimes:
        _COMMON_YDL_OPTS['js_runtimes'] = js_runtimes

    # --- Smart retry: probe both configs and keep the one offering the best
    # --- quality. YouTube silently limits some clients/cookie states to the
    # --- muxed 360p format; without this check we'd process mush without warning.
    probe_results = []  # (attempt_name, attempt_opts, info, max_height)
    last_error = None
    for attempt_name, attempt_opts in [
        ("cookie-auth", _COMMON_YDL_OPTS),
        ("anonymous-fallback", {
            **_COMMON_YDL_OPTS,
            'cookiefile': None,
            # Default clients here too (android_vr/web_safari) — see note above.
            'extractor_args': {},
        }),
    ]:
        try:
            print(f"🔄 Trying download mode: {attempt_name}...")
            with JOB_REPORTER.operation(
                f"yt-dlp metadata probe ({attempt_name})",
                timeout_seconds=YTDLP_PROBE_STALL_SECONDS,
                message=f"Reading YouTube metadata ({attempt_name}).",
                category="download",
            ):
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    probe = ydl.extract_info(url, download=False)
            # Check if we actually got video formats (not just images/storyboards)
            formats = probe.get('formats') or []
            video_formats = [
                f for f in formats
                if f.get('vcodec', 'none') != 'none'
                and f.get('protocol') != 'mhtml'
                and f.get('ext') != 'mhtml'
            ]
            if not video_formats:
                print(f"⚠️ {attempt_name}: No video formats found, trying next mode...")
                continue
            max_height = max((f.get('height') or 0) for f in video_formats)
            print(f"✅ {attempt_name}: Found {len(video_formats)} video formats (best: {max_height}p)")
            probe_results.append((attempt_name, attempt_opts, probe, max_height))
            if max_height >= 1080:
                break
            print(f"⚠️ {attempt_name}: best offered is only {max_height}p — probing next mode for higher quality...")
        except Exception as e:
            last_error = e
            print(f"⚠️ {attempt_name} failed: {e}")

    if not probe_results:
        if last_error is not None:
            print("🚨 YOUTUBE DOWNLOAD ERROR 🚨", file=sys.stderr)

            error_msg = f"""

❌ ================================================================= ❌
❌ FATAL ERROR: YOUTUBE DOWNLOAD FAILED
❌ ================================================================= ❌

REASON: YouTube has blocked the download request (Error 429/Unavailable).
        This is likely a temporary IP ban on this server.

👇 SOLUTION FOR USER 👇
---------------------------------------------------------------------
1. Download the video manually to your computer.
2. Use the 'Upload Video' tab in this app to process it.
---------------------------------------------------------------------

Technical Details: {str(last_error)}
            """
            print(error_msg, file=sys.stdout)
            print(error_msg, file=sys.stderr)
            sys.stdout.flush()
            sys.stderr.flush()
            time.sleep(0.5)
            raise last_error
        print("❌ All download modes failed. No video formats available.")
        raise SystemExit(1)

    attempt_name, attempt_opts, info, best_height = max(probe_results, key=lambda item: item[3])
    _COMMON_YDL_OPTS.update(attempt_opts)
    print(f"🎯 Downloading via {attempt_name} (best available: {best_height}p)")
    if 0 < best_height < 720:
        JOB_REPORTER.warning(
            f"YouTube only offered {best_height}p for this video — clips will look soft. "
            "Fix: re-export cookies from a FRESH incognito session (log in, open "
            "youtube.com/robots.txt, export, close the window and never reuse it) "
            "and update yt-dlp (pip install -U yt-dlp), then re-process.",
            category="download",
        )

    # Publish the duration from the probe: every ETA below the download scales
    # with it, and waiting for the post-download probe would leave the whole
    # download phase without a total estimate.
    probed_duration = info.get('duration')
    if probed_duration:
        JOB_REPORTER.heartbeat(
            "Video metadata loaded.",
            force=True,
            video_duration_seconds=round(float(probed_duration), 3),
            category="download",
        )

    video_title = info.get('title', 'youtube_video')
    sanitized_title = sanitize_filename(video_title)
    
    output_template = os.path.join(output_dir, f'{sanitized_title}.%(ext)s')
    expected_file = os.path.join(output_dir, f'{sanitized_title}.mp4')
    if os.path.exists(expected_file):
        if resume and _reusable_merged_download(expected_file):
            print("✅ Reusing the already complete merged source video.")
            JOB_REPORTER.progress(100.0, message="Download already complete.", important=True, category="download")
            return expected_file, sanitized_title
        os.remove(expected_file)
        print("🗑️  Removed existing file before downloading video")
    
    best_format = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/best[ext=mp4]/best'
    hd_format = ('bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
                 'bestvideo[height<=1080]+bestaudio/best[height<=1080][ext=mp4]/best')

    # YouTube answers a mid-stream 403 when it refuses to keep serving a request.
    # Repeating it unchanged only earns the same 403, so every fallback drops one
    # of the two things that provoke it: the multi-gigabyte 4K format, and the
    # signed-in session. A short pause between attempts lets the block expire.
    download_attempts = [(_COMMON_YDL_OPTS, best_format, f"best available ({best_height}p)")]
    if best_height > 1080:
        download_attempts.append((_COMMON_YDL_OPTS, hd_format, "1080p"))
    if _COMMON_YDL_OPTS.get('cookiefile'):
        download_attempts.append(
            ({**_COMMON_YDL_OPTS, 'cookiefile': None}, hd_format, "1080p without cookies"))

    for index, (attempt_opts, format_spec, attempt_label) in enumerate(download_attempts):
        ydl_opts = {
            **attempt_opts,
            'format': format_spec,
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            # A resume must reuse the format files a killed run already finished.
            # Overwriting them would re-download gigabytes just to redo the merge.
            'overwrites': not resume,
            'continuedl': True,
        }

        try:
            with JOB_REPORTER.operation(
                "yt-dlp download",
                timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
                message=f"YouTube download worker started ({attempt_label}).",
                category="download",
            ):
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            break
        except Exception as e:
            is_last_attempt = index == len(download_attempts) - 1
            if is_last_attempt or not _is_youtube_refusal(e):
                raise
            next_label = download_attempts[index + 1][2]
            JOB_REPORTER.warning(
                f"YouTube refused the download ({attempt_label}): {e}. "
                f"Retrying with {next_label} in {YTDLP_REFUSAL_BACKOFF_SECONDS}s.",
                category="download",
            )
            print(f"⚠️ YouTube refused the {attempt_label} download — falling back to {next_label}.")
            time.sleep(YTDLP_REFUSAL_BACKOFF_SECONDS)


    downloaded_file = os.path.join(output_dir, f'{sanitized_title}.mp4')
    
    if not os.path.exists(downloaded_file):
        for f in os.listdir(output_dir):
            if f.startswith(sanitized_title) and not f.endswith(('.part', '.ytdl', '.json', '.description')):
                downloaded_file = os.path.join(output_dir, f)
                break
    
    step_end_time = time.time()
    JOB_REPORTER.progress(100.0, message="Download complete.", important=True, category="download")
    print(f"✅ Video downloaded in {step_end_time - step_start_time:.2f}s: {downloaded_file}")
    
    return downloaded_file, sanitized_title

def _finalize_clip_passthrough(input_video, final_output_video, progress_callback=None):
    """Finish a clip without reframing (original output, or source already in
    the target aspect). Applies the watermark via FFmpeg overlay if enabled;
    otherwise remuxes with faststart only."""
    if os.path.exists(final_output_video):
        os.remove(final_output_video)
    if progress_callback:
        progress_callback(5.0, "Finalizing clip without reframing...")

    wm_png = None
    if WATERMARK_ENABLED:
        try:
            width, _height = get_video_resolution(input_video)
            wm_png = f"{os.path.splitext(final_output_video)[0]}_wm.png"
            _render_watermark_rgba(width).save(wm_png)
        except Exception as e:
            print(f"⚠️ Watermark skipped (render failed): {e}")
            wm_png = None

    if wm_png:
        command = [
            'ffmpeg', '-y', '-i', input_video, '-i', wm_png,
            '-filter_complex',
            f'[0:v][1:v]overlay=(W-w)/2:(H-h)/2:format=auto,{EVEN_PAD_FILTER}',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-c:a', 'copy', '-movflags', '+faststart', final_output_video,
        ]
    else:
        command = [
            'ffmpeg', '-y', '-i', input_video,
            '-c', 'copy', '-movflags', '+faststart', final_output_video,
        ]

    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
    except subprocess.TimeoutExpired:
        print("\n   ❌ Passthrough finalize timed out after 1800s.")
        return False
    except subprocess.CalledProcessError as e:
        print("\n   ❌ Passthrough finalize failed.")
        print("   Stderr:", (e.stderr or b"").decode(errors="replace"))
        return False
    finally:
        if wm_png and os.path.exists(wm_png):
            os.remove(wm_png)

    print(f"   ✅ Clip saved to {final_output_video}")
    if progress_callback:
        progress_callback(100.0, f"Saved {os.path.basename(final_output_video)}")
    return True


def _render_clip(input_video, final_output_video, output_format="vertical", layout_style="smart", progress_callback=None):
    """Route a cut clip through the right renderer for the chosen output format.
    Legacy ``auto``/``horizontal`` values are accepted for saved jobs and map
    to the explicit ``vertical``/``original`` choices."""
    output_format = normalize_output_format(output_format)
    if output_format == "original":
        return _finalize_clip_passthrough(input_video, final_output_video, progress_callback)
    aspect = output_aspect_ratio(output_format)
    return process_video_to_vertical(input_video, final_output_video, progress_callback,
                                     aspect_ratio=aspect, layout_style=layout_style)


def _full_render_filename(output_dir, video_title, output_format):
    suffix = normalize_output_format(output_format)
    return os.path.join(output_dir, f"{video_title}_{suffix}.mp4")


def process_video_to_vertical(input_video, final_output_video, progress_callback=None, aspect_ratio=ASPECT_RATIO, layout_style="smart"):
    """
    Core logic to convert horizontal video to vertical using scene detection and Active Speaker Tracking (MediaPipe).
    """
    script_start_time = time.time()

    # Define temporary file paths based on the output name
    base_name = os.path.splitext(final_output_video)[0]
    temp_video_output = f"{base_name}_temp_video.mp4"
    temp_audio_output = f"{base_name}_temp_audio.aac"

    # Clean up previous temp files if they exist
    if os.path.exists(temp_video_output): os.remove(temp_video_output)
    if os.path.exists(temp_audio_output): os.remove(temp_audio_output)
    if os.path.exists(final_output_video): os.remove(final_output_video)

    # Smart source detection: if the source is already in the target aspect
    # (e.g. phone footage for 9:16), reframing would only crop it — pass it
    # through untouched instead.
    src_width, src_height = get_video_resolution(input_video)
    if src_width and src_height and abs((src_width / src_height) - aspect_ratio) < 0.02:
        print(f"🎬 Source already matches target aspect ({src_width}x{src_height}) — skipping reframing.")
        return _finalize_clip_passthrough(input_video, final_output_video, progress_callback)

    print(f"🎬 Processing clip: {input_video}")
    print("   Step 1: Detecting scenes...")
    if progress_callback:
        progress_callback(2.0, "Detecting scenes...")
    scenes, fps = detect_scenes(input_video)
    
    if not scenes:
        print("   ℹ️ No cuts detected. Analyzing the full clip as one scene.")
        # A continuous shot is valid input: treat the whole clip as one scene.
        cap = cv2.VideoCapture(input_video)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        from scenedetect import FrameTimecode
        scenes = [(FrameTimecode(0, fps), FrameTimecode(total_frames, fps))]

    print(f"   ✅ Found {len(scenes)} scenes.")

    print("\n   🧠 Step 2: Preparing Active Tracking...")
    if progress_callback:
        progress_callback(8.0, "Preparing active tracking...")
    original_width, original_height = src_width, src_height

    OUTPUT_WIDTH, OUTPUT_HEIGHT = fit_even_output_dimensions(
        original_width, original_height, aspect_ratio,
    )

    # Initialize Cameraman
    cameraman = SmoothedCameraman(OUTPUT_WIDTH, OUTPUT_HEIGHT, original_width, original_height, aspect_ratio=aspect_ratio)
    watermark = _build_watermark_blender(OUTPUT_WIDTH, OUTPUT_HEIGHT)
    
    # --- New Strategy: Per-Scene Analysis ---
    print("\n   🤖 Step 3: Analyzing Scenes for Strategy (Single vs Group)...")
    if progress_callback:
        progress_callback(14.0, "Analyzing scene strategy...")
    scene_decisions = analyze_scenes_strategy(input_video, scenes, layout_style=layout_style)
    if len(scene_decisions) != len(scenes):
        # A failed analysis must not guess a tight crop.  GENERAL preserves
        # everyone until a future run can collect valid evidence.
        scene_decisions = [
            SceneLayoutDecision("GENERAL", None, 0.0, "scene analysis unavailable")
            for _ in scenes
        ]
    scene_strategies = [decision.strategy for decision in scene_decisions]
    scene_split_centers = [decision.split_centers for decision in scene_decisions]
    scene_confidences = [decision.confidence for decision in scene_decisions]

    # Only low-confidence, short GENERAL islands may be widened.  Confident
    # close-ups and long SPLIT shots are source edits and must survive.
    scene_seconds = [
        max(0.0, (s_end.get_frames() - s_start.get_frames()) / max(float(fps or 0), 1.0))
        for s_start, s_end in scenes
    ]
    smoothed = smooth_scene_strategies(
        scene_strategies,
        scene_seconds,
        scene_confidences=scene_confidences,
    )
    if smoothed != scene_strategies:
        flips = sum(1 for a, b in zip(smoothed, scene_strategies) if a != b)
        print(f"   🧘 Stabilized layout plan: {flips} scene(s) smoothed to avoid layout flicker.")
    scene_strategies, scene_split_centers = inherit_split_centers(smoothed, scene_split_centers)

    layout_seconds = {}
    for strategy, duration in zip(scene_strategies, scene_seconds):
        layout_seconds[strategy] = layout_seconds.get(strategy, 0.0) + duration
    total_planned_seconds = max(sum(scene_seconds), 0.001)
    layout_summary = ", ".join(
        f"{seconds:.1f}s {name} ({seconds / total_planned_seconds:.0%})"
        for name, seconds in sorted(layout_seconds.items())
    )
    print(f"   🎛️ Layout plan ({layout_style}): {layout_summary}")
    for index, ((scene_start, scene_end), strategy, confidence, decision) in enumerate(zip(
        scenes,
        scene_strategies,
        scene_confidences,
        scene_decisions,
    )):
        start_seconds = scene_start.get_frames() / max(float(fps or 0), 1.0)
        end_seconds = scene_end.get_frames() / max(float(fps or 0), 1.0)
        reason = decision.reason if strategy == decision.strategy else f"safe smoothing from {decision.strategy}"
        observed_counts = decision.person_counts or decision.face_counts
        observed_people = (
            sorted(observed_counts)[len(observed_counts) // 2]
            if observed_counts else 0
        )
        print(
            f"      {index + 1:02d} {start_seconds:06.2f}-{end_seconds:06.2f}s "
            f"{strategy:<7} people={observed_people} confidence={confidence:.2f} | {reason}"
        )
    
    print("\n   ✂️ Step 4: Processing video frames...")
    if progress_callback:
        progress_callback(20.0, "Processing video frames...")
    
    command = [
        'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}', '-pix_fmt', 'bgr24',
        '-r', str(fps), '-i', '-', '-c:v', 'libx264',
        '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
        '-an', temp_video_output
    ]

    ffmpeg_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    cap = cv2.VideoCapture(input_video)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    frame_number = 0
    current_scene_index = 0
    
    # Pre-calculate scene boundaries
    scene_boundaries = []
    for s_start, s_end in scenes:
        scene_boundaries.append((s_start.get_frames(), s_end.get_frames()))

    # Seconds, not frame counts: the source may be 24, 30, 50 or 60 fps.
    # HOLD is a first-class state and therefore never invokes YOLO fallback.
    speaker_tracker = SpeakerTracker(
        stabilization_seconds=2.0,
        cooldown_seconds=3.0,
        lost_timeout_seconds=2.0,
        fallback_stabilization_seconds=1.0,
    )
    previous_scene_index = None
    fps_value = max(float(fps or 0), 1.0)
    artificial_switches = 0
    scene_had_target = False

    last_progress_emit = time.time()
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # Update Scene Index
            while current_scene_index < len(scene_boundaries) - 1:
                _, end_f = scene_boundaries[current_scene_index]
                if frame_number < end_f:
                    break
                current_scene_index += 1

            # Determine Strategy for current frame based on scene
            current_strategy = scene_strategies[current_scene_index] if current_scene_index < len(scene_strategies) else 'TRACK'
            is_scene_start = current_scene_index != previous_scene_index
            if is_scene_start:
                # A source cut is a legitimate new composition.  Stale face
                # IDs, fallback candidates and camera centers must not cross it.
                speaker_tracker.reset()
                cameraman.reset()
                scene_had_target = False
                previous_scene_index = current_scene_index

            # Apply Strategy
            split_centers = (
                scene_split_centers[current_scene_index]
                if current_strategy == 'SPLIT' and current_scene_index < len(scene_split_centers)
                else None
            )
            if current_strategy == 'SPLIT' and split_centers:
                # Two-person shot -> both people large, stacked (9:16) or
                # side by side (1:1). Fixed per scene = calm framing.
                output_frame = create_split_frame(
                    frame, OUTPUT_WIDTH, OUTPUT_HEIGHT, split_centers,
                    stacked=(OUTPUT_HEIGHT > OUTPUT_WIDTH),
                )

            elif current_strategy == 'GENERAL' or current_strategy == 'SPLIT':
                # "Plano General" -> Blur Background + Fit Width
                # (also the fallback for SPLIT scenes without face centers)
                output_frame = create_general_frame(frame, OUTPUT_WIDTH, OUTPUT_HEIGHT)

            else:
                # "Single Speaker" -> Track & Crop
                if is_scene_start or frame_number % 2 == 0:
                    timestamp_seconds = frame_number / fps_value
                    candidates = detect_face_candidates(frame)
                    target_decision = speaker_tracker.get_target(
                        candidates,
                        timestamp_seconds,
                        original_width,
                    )
                    if target_decision.state == TargetState.TARGET:
                        if scene_had_target and target_decision.reason == "replacement face stayed stable":
                            artificial_switches += 1
                        cameraman.update_target(target_decision.box)
                        scene_had_target = True
                    elif target_decision.state == TargetState.LOST:
                        # LOST means the previous subject is genuinely gone.
                        # HOLD intentionally does nothing and can never reach
                        # this fallback branch.
                        person_box = detect_person_yolo(frame)
                        fallback_decision = speaker_tracker.consider_person_fallback(
                            person_box,
                            timestamp_seconds,
                            original_width,
                        )
                        if fallback_decision.state == TargetState.TARGET:
                            if scene_had_target and fallback_decision.reason == "stable YOLO fallback target":
                                artificial_switches += 1
                            cameraman.update_target(fallback_decision.box)
                            scene_had_target = True

                x1, y1, x2, y2 = cameraman.get_crop_box(force_snap=is_scene_start)
                if y2 > y1 and x2 > x1:
                    cropped = frame[y1:y2, x1:x2]
                    output_frame = cv2.resize(cropped, (OUTPUT_WIDTH, OUTPUT_HEIGHT))
                else:
                    output_frame = cv2.resize(frame, (OUTPUT_WIDTH, OUTPUT_HEIGHT))

            if watermark is not None:
                output_frame = _apply_watermark(output_frame, watermark)

            ffmpeg_process.stdin.write(output_frame.tobytes())
            frame_number += 1
            if progress_callback and (time.time() - last_progress_emit >= 2.0 or frame_number == total_frames):
                frame_progress = 20.0 + (70.0 * (frame_number / max(total_frames, 1)))
                progress_callback(frame_progress, f"Processing video frames... {frame_number}/{total_frames}")
                last_progress_emit = time.time()

        ffmpeg_process.stdin.close()
        stderr_output = ffmpeg_process.stderr.read().decode()
        ffmpeg_process.wait()
    finally:
        cap.release()
        # If the frame loop aborted (exception/cancel), don't leak the encoder subprocess.
        if ffmpeg_process.poll() is None:
            ffmpeg_process.terminate()
            try:
                ffmpeg_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ffmpeg_process.kill()

    print(f"   🎥 Artificial camera switches: {artificial_switches}")

    if ffmpeg_process.returncode != 0:
        print("\n   ❌ FFmpeg frame processing failed.")
        print("   Stderr:", stderr_output)
        return False

    print("\n   🔊 Step 5: Extracting audio...")
    if progress_callback:
        progress_callback(92.0, "Extracting audio...")
    extract_audio_track(input_video, temp_audio_output, warn=JOB_REPORTER.warning)

    print("\n   ✨ Step 6: Merging...")
    if progress_callback:
        progress_callback(97.0, "Merging output...")
    if os.path.exists(temp_audio_output):
        merge_command = [
            'ffmpeg', '-y', '-i', temp_video_output, '-i', temp_audio_output,
            '-c:v', 'copy', '-c:a', 'copy', '-movflags', '+faststart', final_output_video
        ]
    else:
         merge_command = [
            'ffmpeg', '-y', '-i', temp_video_output,
            '-c:v', 'copy', '-movflags', '+faststart', final_output_video
        ]
        
    try:
        subprocess.run(merge_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
        print(f"   ✅ Clip saved to {final_output_video}")
        if progress_callback:
            progress_callback(100.0, f"Saved {os.path.basename(final_output_video)}")
    except subprocess.TimeoutExpired:
        print("\n   ❌ Final merge timed out after 1800s.")
        return False
    except subprocess.CalledProcessError as e:
        print("\n   ❌ Final merge failed.")
        print("   Stderr:", e.stderr.decode())
        return False

    # Clean up temp files
    if os.path.exists(temp_video_output): os.remove(temp_video_output)
    if os.path.exists(temp_audio_output): os.remove(temp_audio_output)
    
    return True

def transcribe_video(video_path, video_duration=None):
    print("🎙️  Transcribing video with Faster-Whisper (CPU Optimized)...")
    from faster_whisper import WhisperModel
    from subtitles import get_whisper_config, WHISPER_TRANSCRIBE_PARAMS, merge_continuation_words

    cfg = get_whisper_config()
    model = WhisperModel(cfg["model_size"], device=cfg["device"], compute_type=cfg["compute_type"])

    segments, info = model.transcribe(video_path, **WHISPER_TRANSCRIBE_PARAMS)
    
    print(f"   Detected language '{info.language}' with probability {info.language_probability:.2f}")
    
    # Convert to openai-whisper compatible format
    transcript_segments = []
    full_text = ""
    last_emit = time.time()
    last_bucket = -1

    for segment in segments:
        seg_dict = {
            'text': segment.text,
            'start': segment.start,
            'end': segment.end,
            'words': []
        }
        
        if segment.words:
            # Merge continuation fragments (tokens without a leading space belong
            # to the previous word) so compound words stay intact downstream.
            raw_words = [
                {
                    'word': word.word,
                    'start': word.start,
                    'end': word.end,
                    'probability': word.probability
                }
                for word in segment.words
            ]
            seg_dict['words'] = merge_continuation_words(raw_words)
        
        transcript_segments.append(seg_dict)
        full_text += segment.text + " "
        if video_duration:
            progress_percent = min(100.0, (float(segment.end) / max(video_duration, 1.0)) * 100.0)
            bucket = int(progress_percent // 2)
            if time.time() - last_emit >= 1.0 or bucket != last_bucket:
                JOB_REPORTER.progress(
                    progress_percent,
                    message=f"Transcribing audio... {progress_percent:.1f}%",
                    important=bucket % 5 == 0,
                    category="transcribe",
                )
                last_emit = time.time()
                last_bucket = bucket
        
    JOB_REPORTER.progress(100.0, message="Transcription complete.", important=True, category="transcribe")
    return {
        'text': full_text.strip(),
        'segments': transcript_segments,
        'language': info.language
    }


RESCUABLE_GEMINI_ERROR_TYPES = {
    "empty_response",
    "blocked_response",
    "invalid_response",
}


def _cost_from_worker_error(exc):
    if isinstance(exc, GeminiWorkerError):
        cost = exc.result.get("cost_analysis")
        return cost if isinstance(cost, dict) else None
    return None


def _rescue_gemini_windows(
    mode, batch_windows, *, video_duration, language, output_dir,
    video_title, batch_index, total_batches,
):
    """Retry a response-blocked batch one window at a time, once each.

    Formatting retries cannot repair a safety/empty-body response caused by a
    single window. Isolation keeps the other windows instead of dropping the
    whole batch, while the one-attempt bound prevents runaway API cost.
    """
    successes = []
    failures = []
    costs = []
    attempt_records = []
    for window in batch_windows:
        window_id = str(window.get("id") or "unknown")
        payload = {
            "video_duration": round(float(video_duration), 3),
            "language": language,
            "windows": [window],
        }
        print(f"🩹 Gemini {mode} rescue: {window_id}")
        try:
            result = _call_gemini_worker(
                mode,
                payload,
                output_dir=output_dir or ".",
                video_title=video_title or "analysis",
                strategy="structured-schema",
                batch_index=batch_index,
                total_batches=total_batches,
                attempt=1,
                artifact_suffix=f"rescue_{window_id}",
            )
            successes.append((window, result))
            cost = result.get("cost_analysis")
            if cost:
                costs.append(cost)
            attempt_records.append({
                "stage": mode,
                "batch": batch_index + 1,
                "window_id": window_id,
                "attempt": 1,
                "name": "single-window-rescue",
                "status": "success",
            })
        except Exception as exc:
            failures.append(window_id)
            cost = _cost_from_worker_error(exc)
            if cost:
                costs.append(cost)
            attempt_records.append({
                "stage": mode,
                "batch": batch_index + 1,
                "window_id": window_id,
                "attempt": 1,
                "name": "single-window-rescue",
                "status": "failed",
                "error": str(exc),
                "error_type": getattr(exc, "error_type", "worker_error"),
            })
    return successes, failures, costs, attempt_records


def _gemini_attempt_specs():
    return [
        {"name": "structured-schema", "strategy": "structured-schema"},
        {"name": "strict-json", "strategy": "strict-json"},
        {"name": "json-text-recovery", "strategy": "json-text-recovery"},
    ]


def _run_score_stage(windows, transcript_language, video_duration, output_dir, video_title):
    """Score transcript windows once for both Shorts and long-form planning."""
    attempt_specs = _gemini_attempt_specs()
    attempts = []
    all_costs = []
    scored_windows = []
    scored_input_ids = set()
    skipped_score_ids = set()
    total_score_batches = max(1, math.ceil(len(windows) / GEMINI_SCORE_BATCH_SIZE))

    for batch_index, batch_windows in _iter_batches(windows, GEMINI_SCORE_BATCH_SIZE):
        JOB_REPORTER.progress(
            (batch_index / max(total_score_batches, 1)) * 45.0,
            message=f"Scoring transcript windows... batch {batch_index + 1}/{total_score_batches}",
            important=True,
            category="analyze",
        )
        batch_payload = {
            "video_duration": round(float(video_duration), 3),
            "language": transcript_language,
            "windows": batch_windows,
        }
        batch_result = None
        last_error = None
        last_error_type = None
        for attempt_number, attempt in enumerate(attempt_specs[:GEMINI_MAX_ATTEMPTS], start=1):
            print(f"🤖  Gemini scoring attempt {attempt_number}/{GEMINI_MAX_ATTEMPTS}: batch {batch_index + 1}/{total_score_batches} ({attempt['name']})")
            try:
                worker_result = _call_gemini_worker(
                    "score",
                    batch_payload,
                    output_dir=output_dir or ".",
                    video_title=video_title or "analysis",
                    strategy=attempt["strategy"],
                    batch_index=batch_index,
                    total_batches=total_score_batches,
                    attempt=attempt_number,
                )
                batch_result = _normalize_scored_windows(worker_result.get("payload", {}), video_duration)
                cost_analysis = worker_result.get("cost_analysis")
                if cost_analysis:
                    all_costs.append(cost_analysis)
                attempts.append({
                    "stage": "score",
                    "batch": batch_index + 1,
                    "attempt": attempt_number,
                    "name": attempt["name"],
                    "status": "success",
                })
                break
            except Exception as exc:
                last_error = str(exc)
                last_error_type = getattr(exc, "error_type", "worker_error")
                failed_cost = _cost_from_worker_error(exc)
                if failed_cost:
                    all_costs.append(failed_cost)
                JOB_REPORTER.warning(
                    f"Gemini scoring attempt {attempt_number} failed for batch {batch_index + 1}/{total_score_batches}: {last_error}",
                    category="gemini",
                    attempt=attempt_number,
                )
                attempts.append({
                    "stage": "score",
                    "batch": batch_index + 1,
                    "attempt": attempt_number,
                    "name": attempt["name"],
                    "status": "failed",
                    "error": last_error,
                    "error_type": last_error_type,
                })
                if last_error_type in {"empty_response", "blocked_response"}:
                    break
                if last_error_type == "api_error" and attempt_number < GEMINI_MAX_ATTEMPTS:
                    time.sleep(min(10.0, float(2 ** attempt_number)))

        if batch_result is not None:
            scored_input_ids.update(str(window.get("id")) for window in batch_windows)
            scored_windows.extend(batch_result)
        elif last_error_type in RESCUABLE_GEMINI_ERROR_TYPES:
            JOB_REPORTER.warning(
                f"Recovering score batch {batch_index + 1}/{total_score_batches} one window at a time.",
                category="gemini",
            )
            rescued, failed_ids, rescue_costs, rescue_attempts = _rescue_gemini_windows(
                "score",
                batch_windows,
                video_duration=video_duration,
                language=transcript_language,
                output_dir=output_dir,
                video_title=video_title,
                batch_index=batch_index,
                total_batches=total_score_batches,
            )
            all_costs.extend(rescue_costs)
            attempts.extend(rescue_attempts)
            for window, worker_result in rescued:
                window_id = str(window.get("id"))
                try:
                    normalized_scores = _normalize_scored_windows(
                        worker_result.get("payload", {}), video_duration,
                    )
                except Exception as exc:
                    failed_ids.append(window_id)
                    JOB_REPORTER.warning(
                        f"Rescued score window {window_id} returned invalid data: {exc}",
                        category="gemini",
                    )
                    continue
                scored_windows.extend(normalized_scores)
                scored_input_ids.add(window_id)
            skipped_score_ids.update(failed_ids)
            if failed_ids:
                JOB_REPORTER.warning(
                    f"Score coverage incomplete: {len(failed_ids)} individual window(s) still failed in batch {batch_index + 1}.",
                    category="gemini",
                )
        elif last_error:
            skipped_score_ids.update(str(window.get("id")) for window in batch_windows)
            JOB_REPORTER.warning(
                f"Skipping score batch {batch_index + 1}/{total_score_batches} after repeated Gemini failures.",
                category="gemini",
            )

    return scored_windows, scored_input_ids, skipped_score_ids, attempts, all_costs


def get_viral_clips(transcript_result, video_duration, output_dir=None, video_title=None):
    print("🤖  Analyzing with Gemini...")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        error_message = "GEMINI_API_KEY not found in environment variables."
        print(f"❌ Error: {error_message}")
        JOB_REPORTER.error(error_message)
        return {
            "clips_data": None,
            "error": error_message,
            "attempts": [],
            "cost_analysis": None,
        }

    if not os.path.exists(GEMINI_WORKER_SCRIPT):
        error_message = f"Gemini worker script not found: {GEMINI_WORKER_SCRIPT}"
        print(f"❌ Error: {error_message}")
        JOB_REPORTER.error(error_message)
        return {
            "clips_data": None,
            "error": error_message,
            "attempts": [],
            "cost_analysis": None,
        }

    print(f"🤖  Initializing Gemini with model: {GEMINI_ANALYSIS_MODEL}")

    words = _extract_words_for_analysis(transcript_result)
    transcript_language = str(transcript_result.get("language") or "unknown")
    windows = _build_transcript_windows(transcript_result, video_duration)
    if output_dir and video_title:
        _save_json_checkpoint(output_dir, video_title, "analysis_windows", {"windows": windows})

    attempt_specs = _gemini_attempt_specs()
    scored_windows, scored_input_ids, skipped_score_ids, attempts, all_costs = _run_score_stage(
        windows, transcript_language, video_duration, output_dir, video_title,
    )

    if not scored_windows:
        error_message = "Gemini could not score any transcript windows."
        print(f"❌ Gemini Error: {error_message}")
        JOB_REPORTER.error(error_message)
        return {
            "clips_data": None,
            "error": error_message,
            "attempts": attempts,
            "cost_analysis": _merge_cost_analyses(all_costs),
            "analysis_coverage": {
                "score_windows_total": len(windows),
                "score_windows_processed": len(scored_input_ids),
                "score_windows_skipped": sorted(skipped_score_ids),
                "detail_windows_total": 0,
                "detail_windows_processed": 0,
                "detail_windows_skipped": [],
            },
            "windows": windows,
            "scored_windows": scored_windows,
        }

    by_id = {}
    for window in sorted(scored_windows, key=lambda item: item.get("score", 0), reverse=True):
        if window["id"] not in by_id:
            by_id[window["id"]] = window
    shortlist_limit, max_clips = _selection_limits(video_duration)
    shortlisted = list(by_id.values())[:shortlist_limit]
    shortlist_lookup = {window["id"]: window for window in windows}
    if output_dir and video_title:
        _save_json_checkpoint(output_dir, video_title, "analysis_shortlist", {
            "windows": shortlisted,
            "shortlist_limit": shortlist_limit,
            "max_clips": max_clips,
            "long_video_policy": float(video_duration) >= GEMINI_LONG_VIDEO_SECONDS,
        })

    detailed_windows = []
    for item in shortlisted:
        full_window = shortlist_lookup.get(item["id"], item)
        detail_start = max(0.0, float(full_window["start"]) - 20.0)
        detail_end = min(float(video_duration), float(full_window["end"]) + 20.0)
        detail_words = _extract_words_for_range(words, detail_start, detail_end)
        detailed_windows.append({
            "id": full_window["id"],
            "start": detail_start,
            "end": detail_end,
            "candidate_score": item.get("score", 0),
            "text": _extract_text_for_range(transcript_result, detail_start, detail_end),
            "words": detail_words,
        })

    collected_clips = []
    detailed_input_ids = set()
    skipped_detail_ids = set()
    total_detail_batches = max(1, math.ceil(len(detailed_windows) / GEMINI_DETAIL_BATCH_SIZE))
    for batch_index, batch_windows in _iter_batches(detailed_windows, GEMINI_DETAIL_BATCH_SIZE):
        batch_progress = 45.0 + ((batch_index / max(total_detail_batches, 1)) * 45.0)
        JOB_REPORTER.progress(
            batch_progress,
            message=f"Detail-analyzing shortlisted windows... batch {batch_index + 1}/{total_detail_batches}",
            important=True,
            category="analyze",
        )
        batch_payload = {
            "video_duration": round(float(video_duration), 3),
            "language": transcript_language,
            "windows": batch_windows,
        }
        batch_result = None
        last_error = None
        last_error_type = None
        for attempt_number, attempt in enumerate(attempt_specs[:GEMINI_MAX_ATTEMPTS], start=1):
            print(f"🤖  Gemini detail attempt {attempt_number}/{GEMINI_MAX_ATTEMPTS}: batch {batch_index + 1}/{total_detail_batches} ({attempt['name']})")
            try:
                worker_result = _call_gemini_worker(
                    "detail",
                    batch_payload,
                    output_dir=output_dir or ".",
                    video_title=video_title or "analysis",
                    strategy=attempt["strategy"],
                    batch_index=batch_index,
                    total_batches=total_detail_batches,
                    attempt=attempt_number,
                )
                batch_result = worker_result.get("payload", {})
                cost_analysis = worker_result.get("cost_analysis")
                if cost_analysis:
                    all_costs.append(cost_analysis)
                attempts.append({
                    "stage": "detail",
                    "batch": batch_index + 1,
                    "attempt": attempt_number,
                    "name": attempt["name"],
                    "status": "success",
                })
                break
            except Exception as e:
                last_error = str(e)
                last_error_type = getattr(e, "error_type", "worker_error")
                failed_cost = _cost_from_worker_error(e)
                if failed_cost:
                    all_costs.append(failed_cost)
                JOB_REPORTER.warning(
                    f"Gemini detail attempt {attempt_number} failed for batch {batch_index + 1}/{total_detail_batches}: {last_error}",
                    category="gemini",
                    attempt=attempt_number,
                )
                attempts.append({
                    "stage": "detail",
                    "batch": batch_index + 1,
                    "attempt": attempt_number,
                    "name": attempt["name"],
                    "status": "failed",
                    "error": last_error,
                    "error_type": last_error_type,
                })
                if last_error_type in {"empty_response", "blocked_response"}:
                    break
                if last_error_type == "api_error" and attempt_number < GEMINI_MAX_ATTEMPTS:
                    time.sleep(min(10.0, float(2 ** attempt_number)))
        if batch_result is not None and isinstance(batch_result.get("shorts"), list):
            detailed_input_ids.update(str(window.get("id")) for window in batch_windows)
            collected_clips.extend(batch_result["shorts"])
        elif last_error_type in RESCUABLE_GEMINI_ERROR_TYPES:
            JOB_REPORTER.warning(
                f"Recovering detail batch {batch_index + 1}/{total_detail_batches} one window at a time.",
                category="gemini",
            )
            rescued, failed_ids, rescue_costs, rescue_attempts = _rescue_gemini_windows(
                "detail",
                batch_windows,
                video_duration=video_duration,
                language=transcript_language,
                output_dir=output_dir,
                video_title=video_title,
                batch_index=batch_index,
                total_batches=total_detail_batches,
            )
            all_costs.extend(rescue_costs)
            attempts.extend(rescue_attempts)
            for window, worker_result in rescued:
                payload = worker_result.get("payload") or {}
                if isinstance(payload.get("shorts"), list):
                    collected_clips.extend(payload["shorts"])
                    detailed_input_ids.add(str(window.get("id")))
                else:
                    failed_ids.append(str(window.get("id")))
            skipped_detail_ids.update(failed_ids)
            if failed_ids:
                JOB_REPORTER.warning(
                    f"Detail coverage incomplete: {len(failed_ids)} individual window(s) still failed in batch {batch_index + 1}.",
                    category="gemini",
                )
        elif last_error:
            skipped_detail_ids.update(str(window.get("id")) for window in batch_windows)
            JOB_REPORTER.warning(
                f"Skipping detail batch {batch_index + 1}/{total_detail_batches} after repeated Gemini failures.",
                category="gemini",
            )

    if not collected_clips:
        error_message = "Gemini did not produce any valid clips from the shortlisted windows."
        print(f"❌ Gemini Error: {error_message}")
        JOB_REPORTER.error(error_message)
        return {
            "clips_data": None,
            "error": error_message,
            "attempts": attempts,
            "cost_analysis": _merge_cost_analyses(all_costs),
            "analysis_coverage": {
                "score_windows_total": len(windows),
                "score_windows_processed": len(scored_input_ids),
                "score_windows_skipped": sorted(skipped_score_ids),
                "detail_windows_total": len(detailed_windows),
                "detail_windows_processed": len(detailed_input_ids),
                "detail_windows_skipped": sorted(skipped_detail_ids),
            },
            "windows": windows,
            "scored_windows": scored_windows,
        }

    normalized_payload = _normalize_shorts_payload(
        {"shorts": collected_clips}, video_duration, words=words,
        max_clips=max_clips,
    )
    cost_analysis = _merge_cost_analyses(all_costs)
    analysis_coverage = {
        "score_windows_total": len(windows),
        "score_windows_processed": len(scored_input_ids),
        "score_windows_skipped": sorted(skipped_score_ids),
        "detail_windows_total": len(detailed_windows),
        "detail_windows_processed": len(detailed_input_ids),
        "detail_windows_skipped": sorted(skipped_detail_ids),
    }
    normalized_payload["analysis_coverage"] = analysis_coverage
    if cost_analysis:
        normalized_payload["cost_analysis"] = cost_analysis
    JOB_REPORTER.progress(
        95.0,
        message=f"Gemini analysis complete. Found {len(normalized_payload['shorts'])} candidate clips.",
        important=True,
        category="analyze",
        analysis_coverage=analysis_coverage,
    )
    return {
        "clips_data": normalized_payload,
        "error": None,
        "attempts": attempts,
        "cost_analysis": cost_analysis,
        "analysis_coverage": analysis_coverage,
        "windows": windows,
        "scored_windows": scored_windows,
    }


def _longform_target_range(video_duration):
    duration = max(0.0, float(video_duration))
    warnings = []
    if duration < LONGFORM_MIN_SOURCE_SECONDS:
        target_min = min(LONGFORM_TARGET_MIN_SECONDS, duration * 0.60)
        target_max = min(LONGFORM_TARGET_MAX_SECONDS, duration * 0.80)
        warnings.append("scaled_target_for_short_source")
    else:
        target_min = min(LONGFORM_TARGET_MIN_SECONDS, duration * 0.90)
        target_max = min(LONGFORM_TARGET_MAX_SECONDS, duration)
    target_min = max(LONGFORM_MIN_SEGMENT_SECONDS, target_min)
    target_max = max(target_min, target_max)
    return round(target_min, 3), round(target_max, 3), warnings


def _longform_analysis_coverage(windows, scored_windows, skipped_score_ids, *, plan_attempted):
    scored_ids = {str(item.get("id")) for item in scored_windows or []}
    return {
        "score_windows_total": len(windows or []),
        "score_windows_processed": len(scored_ids),
        "score_windows_skipped": sorted(str(item) for item in (skipped_score_ids or [])),
        "longform_plan_attempted": bool(plan_attempted),
    }


def get_longform_plan(
    transcript_result,
    video_duration,
    *,
    output_dir,
    video_title,
    windows=None,
    scored_windows=None,
):
    """Score the full transcript and ask Gemini for one coherent edit plan."""
    transcript_language = str(transcript_result.get("language") or "unknown")
    words = _extract_words_for_analysis(transcript_result)
    windows = list(windows or _build_transcript_windows(transcript_result, video_duration))
    reused_scoring = scored_windows is not None
    scored_windows = list(scored_windows) if scored_windows is not None else None
    attempts = []
    all_costs = []
    skipped_score_ids = set()

    preflight_error = None
    if not os.getenv("GEMINI_API_KEY"):
        preflight_error = "GEMINI_API_KEY not found in environment variables."
    elif not os.path.exists(GEMINI_WORKER_SCRIPT):
        preflight_error = f"Gemini worker script not found: {GEMINI_WORKER_SCRIPT}"
    if preflight_error:
        available_scores = scored_windows or []
        return {
            "plan_data": None,
            "error": preflight_error,
            "attempts": attempts,
            "cost_analysis": None,
            "analysis_coverage": _longform_analysis_coverage(
                windows, available_scores, skipped_score_ids, plan_attempted=False,
            ),
            "windows": windows,
            "scored_windows": available_scores,
        }

    if scored_windows is None:
        scored_windows, _processed, skipped_score_ids, score_attempts, score_costs = _run_score_stage(
            windows, transcript_language, video_duration, output_dir, video_title,
        )
        attempts.extend(score_attempts)
        all_costs.extend(score_costs)
    _save_json_checkpoint(output_dir, video_title, "longform_scores", {
        "windows": windows,
        "scored_windows": scored_windows,
    })
    if not scored_windows:
        error_message = "Gemini could not score transcript windows for a long-form plan."
        return {
            "plan_data": None,
            "error": error_message,
            "attempts": attempts,
            "cost_analysis": _merge_cost_analyses(all_costs),
            "analysis_coverage": _longform_analysis_coverage(
                windows, scored_windows, skipped_score_ids, plan_attempted=False,
            ),
            "windows": windows,
            "scored_windows": scored_windows,
        }

    score_by_id = {}
    for item in sorted(scored_windows, key=lambda candidate: candidate.get("score", 0), reverse=True):
        score_by_id.setdefault(str(item.get("id")), item)
    planning_windows = []
    for window in windows:
        score = score_by_id.get(str(window.get("id")), {})
        planning_windows.append({
            "id": window.get("id"),
            "start": window.get("start"),
            "end": window.get("end"),
            "score": int(score.get("score", 0) or 0),
            "reason": str(score.get("reason") or ""),
            "text": str(window.get("text") or ""),
        })

    target_min, target_max, target_warnings = _longform_target_range(video_duration)
    payload = {
        "video_duration": round(float(video_duration), 3),
        "language": transcript_language,
        "windows": planning_windows,
        "target_min_seconds": target_min,
        "target_max_seconds": target_max,
        "min_segment_seconds": LONGFORM_MIN_SEGMENT_SECONDS,
        "max_segment_seconds": LONGFORM_MAX_SEGMENT_SECONDS,
    }
    last_error = None
    plan_progress = 96.0 if reused_scoring else 45.0
    normalize_progress = 98.0 if reused_scoring else 90.0
    complete_progress = 99.0 if reused_scoring else 95.0
    JOB_REPORTER.progress(
        plan_progress, message="Planning a coherent long-form story...", important=True, category="analyze",
    )
    for attempt_number, attempt in enumerate(_gemini_attempt_specs()[:GEMINI_MAX_ATTEMPTS], start=1):
        try:
            worker_result = _call_gemini_worker(
                "longform_plan",
                payload,
                output_dir=output_dir,
                video_title=video_title,
                strategy=attempt["strategy"],
                batch_index=0,
                total_batches=1,
                attempt=attempt_number,
                timeout_seconds=GEMINI_LONGFORM_TIMEOUT_SECONDS,
            )
            cost = worker_result.get("cost_analysis")
            if cost:
                all_costs.append(cost)
            JOB_REPORTER.progress(
                normalize_progress, message="Validating long-form story and cut boundaries...", category="analyze",
            )
            normalized = longform.normalize_longform_plan(
                worker_result.get("payload"),
                video_duration,
                words=words,
                min_segment_seconds=LONGFORM_MIN_SEGMENT_SECONDS,
                max_segment_seconds=LONGFORM_MAX_SEGMENT_SECONDS,
                merge_gap_seconds=LONGFORM_MERGE_GAP_SECONDS,
                target_min_seconds=target_min,
                target_max_seconds=target_max,
                max_segments=LONGFORM_MAX_SEGMENTS,
                cold_open=LONGFORM_COLD_OPEN,
                cold_open_max_seconds=LONGFORM_COLD_OPEN_MAX_SECONDS,
            )
            normalized["warnings"] = list(dict.fromkeys(target_warnings + normalized.get("warnings", [])))
            attempts.append({
                "stage": "longform_plan",
                "attempt": attempt_number,
                "name": attempt["name"],
                "status": "success",
                "viable": normalized.get("viable"),
            })
            checkpoint = {
                "plan_data": normalized,
                "attempts": attempts,
                "cost_analysis": _merge_cost_analyses(all_costs),
            }
            _save_json_checkpoint(output_dir, video_title, "longform_plan", checkpoint)
            JOB_REPORTER.progress(
                complete_progress,
                message=("Long-form plan ready." if normalized.get("viable") else "Long-form plan needs a deterministic fallback."),
                important=True,
                category="analyze",
            )
            return {
                "plan_data": normalized,
                "error": None if normalized.get("viable") else "Gemini marked the source as not viable for long-form.",
                "attempts": attempts,
                "cost_analysis": _merge_cost_analyses(all_costs),
                "analysis_coverage": _longform_analysis_coverage(
                    windows, scored_windows, skipped_score_ids, plan_attempted=True,
                ),
                "windows": windows,
                "scored_windows": scored_windows,
            }
        except Exception as exc:
            last_error = str(exc)
            cost = _cost_from_worker_error(exc)
            if cost:
                all_costs.append(cost)
            attempts.append({
                "stage": "longform_plan",
                "attempt": attempt_number,
                "name": attempt["name"],
                "status": "failed",
                "error": last_error,
                "error_type": getattr(exc, "error_type", "worker_error"),
            })
            JOB_REPORTER.warning(
                f"Long-form planning attempt {attempt_number} failed: {last_error}",
                category="gemini",
                attempt=attempt_number,
            )

    return {
        "plan_data": None,
        "error": last_error or "Gemini did not return a usable long-form plan.",
        "attempts": attempts,
        "cost_analysis": _merge_cost_analyses(all_costs),
        "analysis_coverage": _longform_analysis_coverage(
            windows, scored_windows, skipped_score_ids, plan_attempted=True,
        ),
        "windows": windows,
        "scored_windows": scored_windows,
    }


def _score_based_longform_fallback(
    transcript_result,
    video_duration,
    *,
    video_title,
    windows,
    scored_windows,
):
    """Build a bounded chronological edit from SCORE output when planning fails."""
    if not windows or not scored_windows:
        return None
    target_min, target_max, target_warnings = _longform_target_range(video_duration)
    language_code = str(transcript_result.get("language") or "en").lower().split("-")[0]
    part_label = {
        "de": "Teil", "en": "Part", "es": "Parte", "fr": "Partie",
        "it": "Parte", "pt": "Parte", "nl": "Deel", "pl": "Część",
        "tr": "Bölüm", "sv": "Del", "da": "Del", "no": "Del",
    }.get(language_code, "Part")
    score_by_id = {}
    for item in sorted(scored_windows, key=lambda candidate: candidate.get("score", 0), reverse=True):
        score_by_id.setdefault(str(item.get("id")), item)

    candidates = [
        {
            "id": str(window.get("id")),
            "start": float(window.get("start", 0)),
            "end": float(window.get("end", 0)),
            "score": int(score_by_id.get(str(window.get("id")), {}).get("score", 0) or 0),
        }
        for window in windows
        if (
            str(window.get("id")) in score_by_id
            and float(window.get("end", 0)) > float(window.get("start", 0))
        )
    ]
    if not candidates:
        return None

    selected = []
    remaining = list(candidates)
    while remaining:
        if not selected:
            chosen = max(remaining, key=lambda item: (item["score"], item["end"] - item["start"]))
        else:
            def _selection_value(item):
                distance = min(
                    min(abs(item["start"] - picked["end"]), abs(picked["start"] - item["end"]))
                    for picked in selected
                )
                return (item["score"] * 10.0) - distance

            chosen = max(remaining, key=_selection_value)
        selected.append(chosen)
        remaining.remove(chosen)
        merged = longform.merge_plan_segments([
            {
                **item,
                "chapter_title": "Highlights",
                "priority": item["score"],
                "continuity_importance": max(30, item["score"]),
                "required": False,
                "role": "body",
            }
            for item in selected
        ], LONGFORM_MERGE_GAP_SECONDS)
        assembled = sum(float(item["end"]) - float(item["start"]) for item in merged)
        if assembled >= target_min:
            break

    selected.sort(key=lambda item: (item["start"], item["end"]))
    ranges = longform.merge_plan_segments([
        {
            **item,
            "chapter_title": f"{part_label} {index + 1}",
            "priority": item["score"],
            "continuity_importance": max(30, item["score"]),
            "required": False,
            "role": "body",
        }
        for index, item in enumerate(selected)
    ], LONGFORM_MERGE_GAP_SECONDS)
    body = [item for item in ranges if item.get("role") != "cold_open"]
    if body:
        body[0]["role"] = "setup"
        body[0]["required"] = True
        body[-1]["role"] = "payoff"
        body[-1]["required"] = True

    strongest = max(candidates, key=lambda item: item["score"])
    teaser_midpoint = (strongest["start"] + strongest["end"]) / 2.0
    teaser_start = max(strongest["start"], teaser_midpoint - 6.0)
    teaser_end = min(strongest["end"], teaser_start + 12.0)
    payload = {
        "viable": True,
        # Keep deterministic copy language-neutral: the source title is known,
        # while fabricating prose in the wrong language would be worse than an
        # empty description followed by the automatically generated chapters.
        "video_title": str(video_title).replace("_", " ").strip()[:100],
        "youtube_description": "",
        "segments": [{
            "start": teaser_start,
            "end": teaser_end,
            "chapter_title": "Intro",
            "priority": strongest["score"],
            "continuity_importance": 100,
            "required": True,
            "role": "cold_open",
            "reason": "Strongest scored moment used as deterministic teaser.",
        }] + body,
    }
    try:
        normalized = longform.normalize_longform_plan(
            payload,
            video_duration,
            words=_extract_words_for_analysis(transcript_result),
            min_segment_seconds=LONGFORM_MIN_SEGMENT_SECONDS,
            max_segment_seconds=LONGFORM_MAX_SEGMENT_SECONDS,
            merge_gap_seconds=LONGFORM_MERGE_GAP_SECONDS,
            target_min_seconds=target_min,
            target_max_seconds=target_max,
            max_segments=LONGFORM_MAX_SEGMENTS,
            cold_open=LONGFORM_COLD_OPEN,
            cold_open_max_seconds=LONGFORM_COLD_OPEN_MAX_SECONDS,
        )
    except Exception as exc:
        JOB_REPORTER.warning(f"Score-based long-form fallback was invalid: {exc}", category="analyze")
        return None
    for index, segment in enumerate(
        (item for item in normalized.get("segments", []) if item.get("role") != "cold_open"),
        start=1,
    ):
        segment["chapter_title"] = f"{part_label} {index}"
    warnings = [
        item for item in target_warnings + ["score_based_fallback"] + normalized.get("warnings", [])
        if item != "fewer_than_three_chapters"
    ]
    if len(longform.build_chapters(normalized.get("segments", []))) < 3:
        warnings.append("fewer_than_three_chapters")
    normalized["warnings"] = list(dict.fromkeys(warnings))
    return normalized if normalized.get("viable") else None


def _render_shorts_clips(
    clips_data,
    input_video,
    output_dir,
    output_format,
    layout_style,
    *,
    video_title,
    weight_done_before=0.0,
    total_weight=None,
):
    """Render the legacy Shorts loop with optional combined-job weighting."""
    shorts = clips_data.get("shorts", [])
    total_clips = len(shorts)
    if not shorts:
        return 0.0
    clip_render_weights = [
        max(0.001, float(item["end"]) - float(item["start"]))
        for item in shorts
    ]
    shorts_weight = sum(clip_render_weights)
    combined_weight = float(total_weight) if total_weight is not None else shorts_weight
    combined_weight = max(0.001, combined_weight)
    if total_weight is None:
        JOB_REPORTER.set_output_seconds(shorts_weight)
    completed_render_weight = 0.0

    for i, clip in enumerate(shorts):
        start = clip["start"]
        end = clip["end"]
        print(f"\n🎬 Processing Clip {i + 1}: {start}s - {end}s")
        print(f"   Title: {clip.get('video_title_for_youtube_short', 'No Title')}")
        clip_filename = clip.get("output_filename", f"{video_title}_clip_{i + 1}.mp4")
        clip_temp_path = os.path.join(output_dir, f"temp_{clip_filename}")
        clip_final_path = os.path.join(output_dir, clip_filename)

        JOB_REPORTER.progress(
            ((float(weight_done_before) + completed_render_weight) / combined_weight) * 100.0,
            message=f"Preparing clip {i + 1}/{total_clips}",
            important=True,
            category="render",
        )
        cut_command = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", input_video,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac",
            clip_temp_path,
        ]
        try:
            with JOB_REPORTER.operation(
                f"FFmpeg clip cut {i + 1}/{total_clips}",
                timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
                message=f"Cutting clip {i + 1}/{total_clips}.",
                category="render",
            ):
                subprocess.run(
                    cut_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=BLOCKING_OPERATION_STALL_SECONDS,
                )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"FFmpeg clip cut timed out after {BLOCKING_OPERATION_STALL_SECONDS}s for {clip_filename}"
            )

        current_render_weight = clip_render_weights[i]
        weight_before_clip = float(weight_done_before) + completed_render_weight

        def _clip_progress(inner_percent, message, clip_index=i, clip_count=total_clips):
            render_percent = (
                weight_before_clip + (current_render_weight * (inner_percent / 100.0))
            ) / combined_weight * 100.0
            JOB_REPORTER.progress(
                render_percent,
                message=f"Rendering clip {clip_index + 1}/{clip_count}: {message}",
                important=inner_percent >= 100.0,
                category="render",
            )

        with JOB_REPORTER.operation(
            f"clip render {i + 1}/{total_clips}",
            timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
            message=f"Rendering clip {i + 1}/{total_clips}.",
            category="render",
        ):
            success = _render_clip(
                clip_temp_path,
                clip_final_path,
                output_format=output_format,
                layout_style=layout_style,
                progress_callback=_clip_progress,
            )
        if not success:
            raise RuntimeError(f"Clip render failed for {clip_filename}")
        completed_render_weight += current_render_weight
        JOB_REPORTER.artifact(
            f"clip_{i + 1}", clip_final_path,
            message=f"Clip {i + 1} ready: {clip_final_path}",
        )
        if os.path.exists(clip_temp_path):
            os.remove(clip_temp_path)
    return shorts_weight


def _run_checked_ffmpeg(command, *, label):
    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=BLOCKING_OPERATION_STALL_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} timed out after {BLOCKING_OPERATION_STALL_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace")[-4000:]
        raise RuntimeError(f"{label} failed: {stderr}") from exc


def _render_longform_video(
    plan,
    input_video,
    output_dir,
    video_title,
    *,
    weight_done_before=0.0,
    total_weight=None,
):
    segments = list(plan.get("segments") or [])
    if not segments:
        raise RuntimeError("Long-form plan has no renderable segments.")
    long_weight = max(0.001, float(plan.get("total_duration") or sum(
        float(item["end"]) - float(item["start"]) for item in segments
    )))
    combined_weight = max(0.001, float(total_weight) if total_weight is not None else long_weight)
    if total_weight is None:
        JOB_REPORTER.set_output_seconds(long_weight)

    try:
        source_width, source_height = get_video_resolution(input_video)
    except Exception:
        source_width, source_height = 0, 0
    filter_complex = None
    if longform.needs_16_9_canvas(source_width, source_height):
        filter_complex = longform.blurred_16_9_filter(
            LONGFORM_CANVAS_WIDTH, LONGFORM_CANVAS_HEIGHT,
        )
        print(
            f"🖼️  Long-form source is {source_width}x{source_height}; "
            f"fitting it into a {LONGFORM_CANVAS_WIDTH}x{LONGFORM_CANVAS_HEIGHT} 16:9 canvas."
        )
    else:
        print(f"🖼️  Long-form source is already 16:9 ({source_width}x{source_height}).")

    temp_segments = [
        os.path.join(output_dir, f"temp_{video_title}_long_seg_{index:03d}.mp4")
        for index in range(1, len(segments) + 1)
    ]
    manifest_path = os.path.join(output_dir, f"temp_{video_title}_long_concat.txt")
    joined_path = os.path.join(output_dir, f"temp_{video_title}_long_joined.mp4")
    final_path = os.path.join(output_dir, f"{video_title}_long_1.mp4")
    completed = 0.0

    for index, (segment, segment_path) in enumerate(zip(segments, temp_segments), start=1):
        segment_duration = max(0.001, float(segment["end"]) - float(segment["start"]))
        progress = float(weight_done_before) + (completed * 0.90)
        JOB_REPORTER.progress(
            (progress / combined_weight) * 100.0,
            message=f"Cutting long-form segment {index}/{len(segments)}",
            important=True,
            category="render",
        )
        command = longform.segment_cut_command(
            input_video,
            segment["start"],
            segment["end"],
            segment_path,
            fade_seconds=LONGFORM_AUDIO_FADE_SECONDS,
            filter_complex=filter_complex,
        )
        with JOB_REPORTER.operation(
            f"FFmpeg long-form cut {index}/{len(segments)}",
            timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
            message=f"Encoding long-form segment {index}/{len(segments)}.",
            category="render",
        ):
            _run_checked_ffmpeg(command, label=f"Long-form segment {index}")
        completed += segment_duration

    _save_text_file(manifest_path, longform.concat_manifest_text(temp_segments))
    JOB_REPORTER.progress(
        ((float(weight_done_before) + long_weight * 0.90) / combined_weight) * 100.0,
        message="Joining long-form segments...",
        important=True,
        category="render",
    )
    with JOB_REPORTER.operation(
        "FFmpeg long-form concat",
        timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
        message="Joining long-form segments.",
        category="render",
    ):
        _run_checked_ffmpeg(
            longform.concat_command(manifest_path, joined_path),
            label="Long-form concat",
        )

    def _finalize_progress(inner_percent, message):
        fraction = 0.94 + (max(0.0, min(100.0, float(inner_percent))) / 100.0 * 0.06)
        JOB_REPORTER.progress(
            ((float(weight_done_before) + long_weight * fraction) / combined_weight) * 100.0,
            message=f"Finalizing long video: {message}",
            category="render",
        )

    with JOB_REPORTER.operation(
        "long-form finalize",
        timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
        message="Finalizing the long video.",
        category="render",
    ):
        success = _finalize_clip_passthrough(joined_path, final_path, _finalize_progress)
    if not success:
        raise RuntimeError("Long-form finalization failed.")

    JOB_REPORTER.artifact("long_1", final_path, message=f"Long video ready: {final_path}")
    for path in temp_segments + [manifest_path, joined_path]:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    return final_path


def _build_longform_metadata_entry(plan, output_filename):
    chapters = longform.build_chapters(plan.get("segments") or [])
    description = str(plan.get("youtube_description") or "")
    total_duration = round(float(plan.get("total_duration") or 0.0), 3)
    return {
        "video_type": "long",
        "output_filename": os.path.basename(output_filename),
        "start": 0.0,
        "end": total_duration,
        "duration": total_duration,
        "title": str(plan.get("video_title") or "Long Video"),
        "youtube_description": description,
        "chapters": chapters,
        "segments": plan.get("segments") or [],
        "description_with_chapters": longform.build_youtube_description(description, chapters),
        "warnings": plan.get("warnings") or [],
        "aspect_ratio": "16:9",
    }


def _longform_result_has_valid_plan(result):
    if not isinstance(result, dict):
        return False
    plan = result.get("plan_data")
    return bool(
        isinstance(plan, dict)
        and plan.get("viable") is True
        and isinstance(plan.get("segments"), list)
        and plan["segments"]
        and float(plan.get("total_duration") or 0) > 0
    )


def _analyze_longform_with_fallback(
    transcript,
    duration,
    *,
    output_dir,
    video_title,
    resume_phase,
    windows=None,
    scored_windows=None,
):
    result_path = os.path.join(output_dir, f"{video_title}_longform_result.json")
    if resume_phase != "analyze":
        checkpoint = _load_json_file(result_path)
        if _longform_result_has_valid_plan(checkpoint):
            JOB_REPORTER.artifact("longform_result", result_path)
            return checkpoint

    result = get_longform_plan(
        transcript,
        duration,
        output_dir=output_dir,
        video_title=video_title,
        windows=windows,
        scored_windows=scored_windows,
    )
    plan = result.get("plan_data")
    if not isinstance(plan, dict) or not plan.get("viable"):
        fallback = _score_based_longform_fallback(
            transcript,
            duration,
            video_title=video_title,
            windows=result.get("windows") or windows or [],
            scored_windows=result.get("scored_windows") or scored_windows or [],
        )
        if fallback:
            result["plan_data"] = fallback
            result["fallback_used"] = "scored_windows"
            JOB_REPORTER.warning(
                "Gemini's narrative plan was unavailable; using the bounded score-based long-form fallback.",
                category="analyze",
            )
    _save_json_file(result_path, result)
    JOB_REPORTER.artifact("longform_result", result_path)
    return result


def _run_video_type_pipeline(
    video_type,
    *,
    transcript,
    duration,
    analysis_result,
    output_dir,
    video_title,
    input_video,
    output_format,
    layout_style,
    resume_phase,
    metadata_file,
    analysis_result_file,
    source_url,
):
    del source_url  # Source provenance already lives in analysis_input.json.
    JOB_REPORTER.stats_excluded_phases = {"analyze", "render"}
    if video_type == "long" and duration < LONGFORM_HARD_MIN_SOURCE_SECONDS:
        raise RuntimeError(
            f"Long Video needs at least {int(LONGFORM_HARD_MIN_SOURCE_SECONDS)} seconds of source material "
            f"(received {int(duration)}s)."
        )

    shorts_data = None
    long_result = None
    long_plan = None
    long_skipped_reason = None

    if video_type == "auto":
        if not analysis_result or resume_phase == "analyze":
            JOB_REPORTER.set_phase("analyze", "Analyzing with Gemini", message="Finding Shorts and long-form material...")
            analysis_result = get_viral_clips(
                transcript,
                duration,
                output_dir=output_dir,
                video_title=video_title,
            )
            _save_json_file(analysis_result_file, analysis_result)
            JOB_REPORTER.artifact("analysis_result", analysis_result_file)
        if isinstance(analysis_result.get("clips_data"), dict):
            shorts = analysis_result["clips_data"].get("shorts")
            if isinstance(shorts, list) and shorts:
                shorts_data = dict(analysis_result["clips_data"])
                shorts_data["shorts"] = [dict(item) for item in shorts]

        if duration >= LONGFORM_MIN_SOURCE_SECONDS:
            long_result = _analyze_longform_with_fallback(
                transcript,
                duration,
                output_dir=output_dir,
                video_title=video_title,
                resume_phase=resume_phase,
                windows=analysis_result.get("windows"),
                scored_windows=analysis_result.get("scored_windows"),
            )
            if _longform_result_has_valid_plan(long_result):
                long_plan = long_result["plan_data"]
        else:
            long_skipped_reason = (
                f"source_too_short ({int(duration)}s < {int(LONGFORM_MIN_SOURCE_SECONDS)}s)"
            )
    else:
        JOB_REPORTER.set_phase("analyze", "Planning long video", message="Building a coherent long-form edit...")
        long_result = _analyze_longform_with_fallback(
            transcript,
            duration,
            output_dir=output_dir,
            video_title=video_title,
            resume_phase=resume_phase,
        )
        if _longform_result_has_valid_plan(long_result):
            long_plan = long_result["plan_data"]

    has_shorts = bool(shorts_data and shorts_data.get("shorts"))
    has_long = bool(long_plan)
    if not has_shorts and not has_long:
        short_error = analysis_result.get("error") if isinstance(analysis_result, dict) else None
        long_error = long_result.get("error") if isinstance(long_result, dict) else None
        details = "; ".join(part for part in (short_error, long_error, long_skipped_reason) if part)
        raise RuntimeError(
            "No viable Shorts or bounded long-form video could be produced"
            + (f": {details}" if details else ".")
        )

    if has_shorts:
        for index, clip in enumerate(shorts_data["shorts"]):
            clip["output_filename"] = f"{video_title}_clip_{index + 1}.mp4"
    long_filename = f"{video_title}_long_1.mp4"
    long_entries = [_build_longform_metadata_entry(long_plan, long_filename)] if has_long else []
    if has_shorts and has_long:
        processing_mode = "clips_and_long"
    elif has_long:
        processing_mode = "long_video"
    else:
        processing_mode = "clips"

    short_error = analysis_result.get("error") if isinstance(analysis_result, dict) else None
    long_error = long_result.get("error") if isinstance(long_result, dict) else None
    analysis_errors = [item for item in (short_error, long_error) if item]
    metadata = {
        "schema_version": 1,
        "processing_mode": processing_mode,
        "video_type": video_type,
        "analysis_status": "success" if not analysis_errors else "partial",
        "analysis_error": "; ".join(analysis_errors) or None,
        "analysis_attempts": (
            (analysis_result.get("attempts", []) if isinstance(analysis_result, dict) else [])
            + (long_result.get("attempts", []) if isinstance(long_result, dict) else [])
        ),
        "analysis_coverage": (
            analysis_result.get("analysis_coverage") if isinstance(analysis_result, dict)
            else long_result.get("analysis_coverage") if isinstance(long_result, dict) else None
        ),
        "longform_analysis_coverage": long_result.get("analysis_coverage") if isinstance(long_result, dict) else None,
        "cost_analysis": _merge_cost_analyses([
            analysis_result.get("cost_analysis") if isinstance(analysis_result, dict) else None,
            long_result.get("cost_analysis") if isinstance(long_result, dict) else None,
        ]),
        "transcript": transcript,
        "shorts": shorts_data.get("shorts", []) if has_shorts else [],
        "long_videos": long_entries,
    }
    if long_skipped_reason:
        metadata["long_video_skipped_reason"] = long_skipped_reason
    _save_json_file(metadata_file, metadata)
    JOB_REPORTER.artifact("metadata", metadata_file)
    JOB_REPORTER.emit(
        "result_mode",
        message=f"Output mode: {processing_mode}",
        processing_mode=processing_mode,
        analysis_status=metadata["analysis_status"],
        analysis_error=metadata["analysis_error"],
    )

    shorts_weight = sum(
        max(0.001, float(item["end"]) - float(item["start"]))
        for item in metadata["shorts"]
    )
    long_weight = float(long_plan.get("total_duration") or 0.0) if has_long else 0.0
    total_weight = max(0.001, shorts_weight + long_weight)
    JOB_REPORTER.set_output_seconds(total_weight)
    JOB_REPORTER.set_phase(
        "render",
        "Rendering selected videos",
        message="Rendering Shorts first, then the long video..." if has_shorts and has_long else "Rendering selected video output...",
    )
    if has_shorts:
        _render_shorts_clips(
            shorts_data,
            input_video,
            output_dir,
            output_format,
            layout_style,
            video_title=video_title,
            weight_done_before=0.0,
            total_weight=total_weight,
        )
    if has_long:
        _render_longform_video(
            long_plan,
            input_video,
            output_dir,
            video_title,
            weight_done_before=shorts_weight,
            total_weight=total_weight,
        )
    return metadata


def _ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def _get_video_duration(video_path):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if fps <= 0:
        return 0.0
    return frame_count / fps


def _find_source_video(resume_dir: str, *, require_audio: bool = False):
    """Locate the downloaded/uploaded source video in a job directory."""
    skip_prefixes = ("temp_", "subtitled_", "hook_", "hooked_", "edited_", "translated_")
    candidates = []
    for path in sorted(glob.glob(os.path.join(resume_dir, "*.mp4"))):
        name = os.path.basename(path)
        if name.startswith(skip_prefixes):
            continue
        if re.search(r"_(?:clip|long)_\d+\.mp4$", name) or name.endswith((
            "_vertical.mp4", "_square.mp4", "_original.mp4",
        )):
            continue
        # yt-dlp leaves per-format files (Title.f625.mp4) behind when it is
        # killed before the merge. The video-only one is the largest file in the
        # directory, so picking by size would resume on a silent video.
        if re.search(r"\.f\d+\.mp4$", name):
            continue
        # FFmpegMergerPP writes to <title>.temp.mp4 and only renames it after a
        # successful mux. Its presence therefore proves the merge was interrupted.
        if name.lower().endswith(".temp.mp4"):
            continue
        streams = _probe_stream_types(path)
        if streams is not None and "video" not in streams:
            continue
        # None means ffprobe could not run, not that audio is missing. The
        # name filters above already excluded the video-only .fNNN fragments,
        # so an unprobeable remaining candidate is kept rather than forcing a
        # needless re-download (see _probe_stream_types).
        if require_audio and streams is not None and "audio" not in streams:
            continue
        candidates.append(path)
    # If several remain, the source is by far the largest one.
    return max(candidates, key=os.path.getsize) if candidates else None


def _probe_stream_types(path: str):
    """Stream types ffprobe finds in a file.

    Returns None when ffprobe itself could not run — the caller must not treat
    that as "file is broken", or a missing ffprobe would delete healthy videos.
    An empty set means ffprobe ran and rejected the file.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            capture_output=True, timeout=60,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in (result.stdout or b"").decode("utf-8", "ignore").splitlines()
        if line.strip()
    }


def _reusable_merged_download(path: str) -> bool:
    """Whether an existing final mp4 can serve a resumed download.

    The final <title>.mp4 only ever appears through a completed single-file
    download or a completed merge (FFmpegMergerPP renames its .temp.mp4 only
    on success), so on an unknown probe result the file is kept: deleting a
    healthy multi-gigabyte download over a probe hiccup is the worse failure
    (see _probe_stream_types). Only a probe that PROVES missing streams
    rejects the file.
    """
    streams = _probe_stream_types(path)
    return streams is None or {"video", "audio"}.issubset(streams)


def _clean_partial_download(resume_dir: str):
    """Drop half-merged mp4 files before a resume re-runs the download.

    yt-dlp's merge writes the final Title.mp4 in place; a job killed mid-merge
    leaves a truncated file that would either fail later or silently lose its
    audio track. The per-format files and .part fragments are kept on purpose so
    the resumed download only redoes the merge.
    """
    removed = []
    for path in sorted(glob.glob(os.path.join(resume_dir, "*.mp4"))):
        name = os.path.basename(path)
        if re.search(r"\.f\d+\.mp4$", name) or re.search(r"_(?:clip|long)_\d+\.mp4$", name):
            continue
        if name.endswith(("_vertical.mp4", "_square.mp4", "_original.mp4")):
            continue
        if name.lower().endswith(".temp.mp4"):
            try:
                os.remove(path)
                removed.append(name)
            except OSError:
                pass
            continue
        streams = _probe_stream_types(path)
        if streams is None:
            print(f"⚠️ Could not probe {name} — keeping it to be safe.")
            continue
        if "video" in streams and "audio" in streams:
            continue
        try:
            os.remove(path)
            removed.append(name)
        except OSError:
            pass
    if removed:
        print(f"🧹 Removed incomplete download artifacts: {', '.join(removed)}")
    return removed


def _load_render_config(output_dir: str) -> dict:
    """Per-job render settings (e.g. output format), persisted so resumes keep
    rendering the same way the job was started."""
    path = os.path.join(output_dir, "render_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _load_resume_context(resume_dir: str, fallback_input: Optional[str] = None):
    analysis_input_files = sorted(glob.glob(os.path.join(resume_dir, "*_analysis_input.json")))
    if not analysis_input_files:
        # The job died before the transcript/analysis checkpoints were written
        # (e.g. frozen mid-transcription). If the source video survived,
        # resume from the transcription phase instead of failing — the
        # pipeline handles transcript=None by transcribing again.
        source_url = None
        state_file = os.path.join(resume_dir, "job_state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    source_url = json.load(f).get("source_url")
            except Exception:
                pass
        source_video = _find_source_video(resume_dir, require_audio=bool(source_url))
        if not source_video and fallback_input and os.path.exists(fallback_input):
            # Local uploads live outside the job directory (uploads/), so
            # nothing can be found in resume_dir. The server passes the
            # persisted upload path back in for exactly this case. ffprobe
            # returning None means it could not run — keep the file then, the
            # policy _probe_stream_types documents.
            streams = _probe_stream_types(fallback_input)
            if streams is None or "video" in streams:
                source_video = fallback_input

        if not source_video:
            # Job died mid-download (only a .part file left, or nothing at
            # all). With a known source URL the pipeline can re-download
            # instead of failing — yt-dlp even continues partial .part files.
            if not source_url:
                raise FileNotFoundError(f"No analysis input file found in {resume_dir}")
            print("🔁 No checkpoints or source video found — re-downloading source for resume.")
            return {
                "output_dir": resume_dir,
                "video_title": None,
                "input_video": None,
                "source_url": source_url,
                "duration": 0.0,
                "transcript": None,
                "analysis_result": None,
                "metadata": None,
            }

        video_title = os.path.splitext(os.path.basename(source_video))[0]

        print(f"🔁 No checkpoints found — resuming from source video: {os.path.basename(source_video)}")
        return {
            "output_dir": resume_dir,
            "video_title": video_title,
            "analysis_input_file": os.path.join(resume_dir, f"{video_title}_analysis_input.json"),
            "analysis_result_file": os.path.join(resume_dir, f"{video_title}_analysis_result.json"),
            "metadata_file": os.path.join(resume_dir, f"{video_title}_metadata.json"),
            "transcript_file": os.path.join(resume_dir, f"{video_title}_transcript.json"),
            "words_file": os.path.join(resume_dir, f"{video_title}_words.json"),
            "input_video": source_video,
            "source_url": source_url,
            "duration": 0.0,
            "transcript": None,
            "analysis_result": None,
            "metadata": None,
        }

    analysis_input_file = analysis_input_files[0]
    video_title = os.path.basename(analysis_input_file).replace("_analysis_input.json", "")
    with open(analysis_input_file, "r", encoding="utf-8") as f:
        analysis_input_payload = json.load(f)

    analysis_result_file = os.path.join(resume_dir, f"{video_title}_analysis_result.json")
    metadata_file = os.path.join(resume_dir, f"{video_title}_metadata.json")
    transcript_file = os.path.join(resume_dir, f"{video_title}_transcript.json")
    words_file = os.path.join(resume_dir, f"{video_title}_words.json")

    transcript = analysis_input_payload.get("transcript")
    if not transcript and os.path.exists(transcript_file):
        with open(transcript_file, "r", encoding="utf-8") as f:
            transcript = json.load(f)

    analysis_result = None
    if os.path.exists(analysis_result_file):
        with open(analysis_result_file, "r", encoding="utf-8") as f:
            analysis_result = json.load(f)

    metadata = None
    if os.path.exists(metadata_file):
        with open(metadata_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)

    input_video = analysis_input_payload.get("input_video")
    source_url = analysis_input_payload.get("source_url")
    if source_url and input_video and os.path.exists(input_video):
        # The checkpoint recorded this path after a successful download, so
        # the probe is only a sanity re-check. Discard the file solely when
        # ffprobe PROVES streams are missing — None means the probe could not
        # run, and clearing on that would force a needless re-download of a
        # healthy file (see _probe_stream_types).
        streams = _probe_stream_types(input_video)
        if streams is not None and not {"video", "audio"}.issubset(streams):
            input_video = None

    return {
        "output_dir": resume_dir,
        "video_title": video_title,
        "analysis_input_file": analysis_input_file,
        "analysis_result_file": analysis_result_file,
        "metadata_file": metadata_file,
        "transcript_file": transcript_file,
        "words_file": words_file,
        "input_video": input_video,
        "source_url": source_url,
        "duration": float(analysis_input_payload.get("video_duration") or 0.0),
        "transcript": transcript,
        "analysis_result": analysis_result,
        "metadata": metadata,
    }


def _analysis_result_has_valid_clips(analysis_result) -> bool:
    if not isinstance(analysis_result, dict):
        return False
    clips_data = analysis_result.get("clips_data")
    if not isinstance(clips_data, dict):
        return False
    shorts = clips_data.get("shorts")
    return isinstance(shorts, list) and len(shorts) > 0


def _analysis_result_has_reusable_scores(analysis_result) -> bool:
    return bool(
        isinstance(analysis_result, dict)
        and isinstance(analysis_result.get("windows"), list)
        and analysis_result.get("windows")
        and isinstance(analysis_result.get("scored_windows"), list)
        and analysis_result.get("scored_windows")
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="AutoCrop-Vertical with Viral Clip Detection.")
    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument('-i', '--input', type=str, help="Path to the input video file.")
    input_group.add_argument('-u', '--url', type=str, help="YouTube URL to download and process.")
    parser.add_argument('-o', '--output', type=str, help="Output directory or file (if processing whole video).")
    parser.add_argument('--keep-original', action='store_true', help="Keep the downloaded YouTube video.")
    parser.add_argument('--skip-analysis', action='store_true', help="Skip AI analysis and convert the whole video (takes precedence over --video-type).")
    parser.add_argument('--resume-dir', type=str, help="Resume a previous job from its output directory.")
    parser.add_argument('--resume-phase', choices=['transcribe', 'analyze', 'render'], help="Force resume from a specific phase.")
    parser.add_argument('--format', dest='output_format', choices=list(OUTPUT_FORMAT_CHOICES), default='vertical',
                        help="Output format: vertical (9:16), original (source geometry), square (1:1). Legacy auto/horizontal values remain accepted.")
    parser.add_argument('--layout', dest='layout_style', choices=list(LAYOUT_STYLES), default='smart',
                        help="Reframing layout: smart (split two-person shots), zoom (speaker zoom only), wide (always blurred wide).")
    parser.add_argument('--video-type', dest='video_type', choices=['shorts', 'long', 'auto'], default='shorts',
                        help="Output type: legacy Shorts, one coherent 16:9 long video, or Smart/Auto mixed output.")
    parser.add_argument('--job-id', type=str, help="Optional job id for structured worker events.")
    args = parser.parse_args()

    if not args.resume_dir and not args.input and not args.url:
        parser.error("You must provide --input, --url, or --resume-dir.")

    script_start_time = time.time()
    reporter = JobReporter(job_id=args.job_id)
    set_job_reporter(reporter)
    _start_keepalive()
    _prevent_windows_sleep()

    try:
        transcript = None
        analysis_result = None
        clips_data = None
        duration = 0.0
        source_url = args.url

        if args.resume_dir:
            output_dir = _ensure_dir(args.resume_dir)
            resume_context = _load_resume_context(output_dir, fallback_input=args.input)
            _render_config = _load_render_config(output_dir)
            output_format = normalize_output_format(
                _render_config.get("output_format") or args.output_format
            )
            layout_style = _render_config.get("layout_style") or args.layout_style
            video_type = _render_config.get("video_type") or args.video_type
            if video_type not in ("shorts", "long", "auto"):
                video_type = "shorts"
            if layout_style not in LAYOUT_STYLES:
                layout_style = "smart"
            input_video = resume_context["input_video"]
            video_title = resume_context["video_title"]
            source_url = resume_context["source_url"]
            duration = resume_context["duration"]
            transcript = resume_context["transcript"]
            analysis_result = resume_context["analysis_result"]
            force_analyze = args.resume_phase == "analyze"
            reusable_analysis = (
                _analysis_result_has_valid_clips(analysis_result)
                if video_type == "shorts"
                else (
                    _analysis_result_has_valid_clips(analysis_result)
                    or _analysis_result_has_reusable_scores(analysis_result)
                )
                if video_type == "auto"
                else True
            )
            if force_analyze or not reusable_analysis:
                analysis_result = None
            if (not input_video or not os.path.exists(input_video)) and source_url:
                # Source video lost (e.g. killed mid-download): re-download
                # instead of failing the resume. yt-dlp resumes .part files.
                reporter.set_phase("download", "Downloading source video",
                                   message="Source video missing — re-downloading for resume...")
                _clean_partial_download(output_dir)
                input_video, video_title = download_youtube_video(source_url, output_dir, resume=True)
            reporter.emit("resume", "Resuming previous job from saved checkpoints.", important=True, resumable=True)
        else:
            output_format = normalize_output_format(args.output_format)
            layout_style = args.layout_style
            video_type = args.video_type
            if args.url:
                if args.output and not args.skip_analysis:
                    output_dir = _ensure_dir(args.output)
                else:
                    if args.output and os.path.isdir(args.output):
                        output_dir = args.output
                    elif args.output and not os.path.isdir(args.output):
                        output_dir = os.path.dirname(args.output) or "."
                    else:
                        output_dir = "."
                # Persist the render settings before the download so a crash-resume keeps them.
                _save_json_file(os.path.join(output_dir, "render_config.json"),
                                {"output_format": output_format, "layout_style": layout_style, "video_type": video_type})
                reporter.set_phase("download", "Downloading source video", message="Starting YouTube download...")
                input_video, video_title = download_youtube_video(args.url, output_dir)
            else:
                input_video = args.input
                video_title = os.path.splitext(os.path.basename(input_video))[0]
                if args.output and not args.skip_analysis:
                    output_dir = _ensure_dir(args.output)
                else:
                    if args.output and os.path.isdir(args.output):
                        output_dir = args.output
                    elif args.output and not os.path.isdir(args.output):
                        output_dir = os.path.dirname(args.output) or os.path.dirname(input_video)
                    else:
                        output_dir = os.path.dirname(input_video)

        if not input_video or not os.path.exists(input_video):
            raise FileNotFoundError(f"Input file not found: {input_video}")

        # Persist render settings so a resume renders exactly like the original run.
        _save_json_file(os.path.join(output_dir, "render_config.json"),
                        {"output_format": output_format, "layout_style": layout_style, "video_type": video_type})
        print(f"🖼️  Output format: {output_format} | Layout: {layout_style} | Video type: {video_type}")

        reporter.artifact("source_video", input_video, message=f"Source video ready: {input_video}")
        duration = duration or _get_video_duration(input_video)
        reporter.emit("heartbeat", message="Source video loaded.", video_duration_seconds=round(float(duration), 3), important=False)

        transcript_file = os.path.join(output_dir, f"{video_title}_transcript.json")
        words_file = os.path.join(output_dir, f"{video_title}_words.json")
        analysis_input_file = os.path.join(output_dir, f"{video_title}_analysis_input.json")
        analysis_result_file = os.path.join(output_dir, f"{video_title}_analysis_result.json")
        metadata_file = os.path.join(output_dir, f"{video_title}_metadata.json")

        if args.skip_analysis and not args.resume_dir:
            reporter.set_phase("render", "Rendering full video", message="Skipping AI analysis and rendering the full video.")
            reporter.set_output_seconds(duration)
            output_file = args.output if args.output else _full_render_filename(
                output_dir, video_title, output_format,
            )
            with reporter.operation(
                "full-video render",
                timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
                message="Full-video renderer started.",
                category="render",
            ):
                success = _render_clip(
                    input_video,
                    output_file,
                    output_format=output_format,
                    layout_style=layout_style,
                    progress_callback=lambda percent, message: reporter.progress(percent, message=message, category="render"),
                )
            if not success:
                raise RuntimeError("Full-video render failed.")
        else:
            if not transcript or args.resume_phase == "transcribe":
                reporter.set_phase("transcribe", "Transcribing audio", message="Starting transcription...")
                with reporter.operation(
                    "transcription",
                    timeout_seconds=max(BLOCKING_OPERATION_STALL_SECONDS, int(duration * 2.0)),
                    message="Transcription worker started.",
                    category="transcribe",
                ):
                    transcript = transcribe_video(input_video, duration)
                _save_json_file(transcript_file, transcript)
                reporter.artifact("transcript", transcript_file)
                words_payload = {"words": _extract_words_for_analysis(transcript)}
                _save_json_file(words_file, words_payload)
                reporter.artifact("words", words_file)

            analysis_input_payload = _build_analysis_input_payload(
                transcript_result=transcript,
                video_duration=duration,
                source_url=source_url,
                input_video=input_video,
            )
            _save_json_file(analysis_input_file, analysis_input_payload)
            print(f"📝 Saved analysis input to {analysis_input_file}")
            reporter.artifact("analysis_input", analysis_input_file)

            if video_type != "shorts":
                _run_video_type_pipeline(
                    video_type,
                    transcript=transcript,
                    duration=duration,
                    analysis_result=analysis_result,
                    output_dir=output_dir,
                    video_title=video_title,
                    input_video=input_video,
                    output_format=output_format,
                    layout_style=layout_style,
                    resume_phase=args.resume_phase,
                    metadata_file=metadata_file,
                    analysis_result_file=analysis_result_file,
                    source_url=source_url,
                )
            else:
                if not analysis_result or args.resume_phase == "analyze":
                    reporter.set_phase("analyze", "Analyzing with Gemini", message="Starting Gemini analysis...")
                    with reporter.operation(
                        "Gemini analysis",
                        timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
                        message="Gemini analysis worker started.",
                        category="analyze",
                    ):
                        analysis_result = get_viral_clips(
                            transcript,
                            duration,
                            output_dir=output_dir,
                            video_title=video_title,
                        )
                    _save_json_file(analysis_result_file, analysis_result)
                    reporter.artifact("analysis_result", analysis_result_file)

                clips_data = analysis_result["clips_data"]

                reporter.set_phase("render", "Rendering clips", message="Starting vertical render...")
                if not clips_data or 'shorts' not in clips_data:
                    print("❌ Failed to identify clips. Converting whole video as fallback.")
                    reporter.set_output_seconds(duration)
                    output_file = _full_render_filename(output_dir, video_title, output_format)
                    with reporter.operation(
                        "fallback render",
                        timeout_seconds=BLOCKING_OPERATION_STALL_SECONDS,
                        message="Fallback renderer started.",
                        category="render",
                    ):
                        success = _render_clip(
                            input_video,
                            output_file,
                            output_format=output_format,
                            layout_style=layout_style,
                            progress_callback=lambda percent, message: reporter.progress(percent, message=message, category="render"),
                        )
                    if not success:
                        raise RuntimeError("Full-video fallback rendering failed.")

                    fallback_metadata = _build_fallback_metadata(
                        video_title=video_title,
                        transcript=transcript,
                        duration=duration,
                        output_filename=output_file,
                        analysis_error=analysis_result["error"],
                        attempts=analysis_result["attempts"],
                        cost_analysis=analysis_result["cost_analysis"],
                    )
                    _save_json_file(metadata_file, fallback_metadata)
                    reporter.artifact("metadata", metadata_file, message=f"Saved fallback metadata to {metadata_file}")
                    reporter.warning("Fallback video rendered after AI analysis failed.")
                else:
                    print(f"🔥 Found {len(clips_data['shorts'])} viral clips!")
                    clips_data['schema_version'] = 1
                    clips_data['processing_mode'] = 'clips'
                    clips_data['analysis_status'] = 'success'
                    clips_data['analysis_attempts'] = analysis_result["attempts"]
                    clips_data['transcript'] = transcript
                    for i, clip in enumerate(clips_data['shorts']):
                        clip['output_filename'] = f"{video_title}_clip_{i+1}.mp4"
                    _save_json_file(metadata_file, clips_data)
                    reporter.artifact("metadata", metadata_file)

                    _render_shorts_clips(
                        clips_data,
                        input_video,
                        output_dir,
                        output_format,
                        layout_style,
                        video_title=video_title,
                    )

        reporter.set_phase("finalize", "Finalizing output", message="Wrapping up artifacts...")
        if args.url and not args.keep_original:
            print(f"🗂️  Keeping downloaded source for resume/retention: {input_video}")
            reporter.warning("Downloaded source video kept for resume support and delayed cleanup.")

        total_time = time.time() - script_start_time
        print(f"\n⏱️  Total execution time: {total_time:.2f}s")
        reporter.summary("completed", f"Job completed in {total_time:.2f}s.", resumable=False)
    except Exception as e:
        print(f"❌ Fatal pipeline error: {e}")
        reporter.error(str(e), resumable=True)
        raise
