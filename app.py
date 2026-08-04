import copy
import os
import sys
import uuid
import subprocess
import threading
import queue
import json
import shutil
import glob
import time
import asyncio
import re
import signal
import zipfile
import hashlib
from datetime import datetime, timezone
from dotenv import load_dotenv
from typing import Dict, Optional, List, Literal
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from s3_uploader import upload_job_artifacts, list_all_clips, upload_actor_to_s3, list_actor_gallery, upload_video_to_gallery, list_video_gallery
from video_formats import CANONICAL_OUTPUT_FORMATS, normalize_output_format

load_dotenv()

# Constants
UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configuration
# Default to 1 if not set, but user can set higher for powerful servers
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "5"))
MAX_FILE_SIZE_MB = 2048  # 2GB limit
JOB_RETENTION_SECONDS = int(os.environ.get("JOB_RETENTION_SECONDS", str(24 * 3600)))
# The worker emits a keepalive heartbeat every 15s, so silence this long really
# means the process is frozen — not just busy inside a long FFmpeg/Whisper call.
HEARTBEAT_STALL_WARNING_SECONDS = int(os.environ.get("JOB_STALL_WARNING_SECONDS", "90"))
HEARTBEAT_STALLED_SECONDS = int(os.environ.get("JOB_STALLED_SECONDS", "240"))
HEARTBEAT_MONITOR_INTERVAL_SECONDS = int(os.environ.get("JOB_STALL_CHECK_INTERVAL_SECONDS", "5"))
# How often a stalled job restarts itself before it waits for the user.
MAX_AUTO_RESUMES = int(os.environ.get("JOB_MAX_AUTO_RESUMES", "2"))
AUTO_RESUME_BACKOFF_SECONDS = [10, 30]
JOB_LOG_LIMIT = int(os.environ.get("JOB_LOG_LIMIT", "4000"))
IMPORTANT_LOG_LIMIT = int(os.environ.get("JOB_IMPORTANT_LOG_LIMIT", "1000"))
EVENT_PREFIX = "__JOB_EVENT__"
JOB_STATE_FILENAME = "job_state.json"
JOB_TOMBSTONE_DIR = os.path.join(OUTPUT_DIR, ".job_tombstones")
ACTIVE_JOB_STATUSES = {"queued", "processing"}
TERMINAL_JOB_STATUSES = {"completed", "failed", "archived"}
os.makedirs(JOB_TOMBSTONE_DIR, exist_ok=True)

THUMBNAIL_SESSION_STATE_DIR = os.path.join(OUTPUT_DIR, ".thumbnail_sessions")
PUBLISH_JOB_STATE_DIR = os.path.join(OUTPUT_DIR, ".publish_jobs")
SAAS_JOB_STATE_FILENAME = "saas_job_state.json"
os.makedirs(THUMBNAIL_SESSION_STATE_DIR, exist_ok=True)
os.makedirs(PUBLISH_JOB_STATE_DIR, exist_ok=True)

# Application State
job_queue = asyncio.Queue()
jobs: Dict[str, Dict] = {}
thumbnail_sessions: Dict[str, Dict] = {}
publish_jobs: Dict[str, Dict] = {}  # {publish_id: {status, result, error, created_at}}
# Semester to limit concurrency to MAX_CONCURRENT_JOBS
concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Serializes writes to job_state.json across the log thread and the run_job coroutine.
_state_write_lock = threading.Lock()

# Per-job registry of subprocesses we control, so cancellation can terminate them.
# job_id -> set[subprocess.Popen]; guarded by job_processes_lock.
job_processes: Dict[str, set] = {}
job_processes_lock = threading.Lock()
job_resume_locks: Dict[str, asyncio.Lock] = {}

# Editing the same clip twice at once used to reuse temporary/output names and
# could corrupt both results. Locks live only for the process lifetime; every
# output name is unique as a second line of defense across restarts/workers.
clip_operation_locks: Dict[tuple[str, int], asyncio.Lock] = {}
job_state_locks: Dict[str, asyncio.Lock] = {}

# TTLs for in-memory dicts that would otherwise grow unbounded.
THUMBNAIL_SESSION_TTL_SECONDS = int(os.environ.get("THUMBNAIL_SESSION_TTL_SECONDS", str(2 * 3600)))
PUBLISH_JOB_TTL_SECONDS = int(os.environ.get("PUBLISH_JOB_TTL_SECONDS", str(3600)))


def _process_group_id(proc: "subprocess.Popen") -> Optional[int]:
    """The worker's own process group, or None if it shares the server's.

    Killing the server's own group would take the API down together with the
    job, so anything but a dedicated group is treated as "no group to kill".
    """
    if os.name == "nt":
        return None
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return None
    return None if pgid == os.getpgid(0) else pgid


def _register_job_process(job_id: str, proc: "subprocess.Popen") -> None:
    # Resolve the group while the process is still alive. Once it is reaped its
    # pid is gone and any surviving FFmpeg children can no longer be located.
    proc._job_pgid = _process_group_id(proc)
    with job_processes_lock:
        job_processes.setdefault(job_id, set()).add(proc)


def _unregister_job_process(job_id: str, proc: "subprocess.Popen") -> None:
    with job_processes_lock:
        procs = job_processes.get(job_id)
        if procs:
            procs.discard(proc)
            if not procs:
                job_processes.pop(job_id, None)


def _kill_process_tree(proc: "subprocess.Popen") -> bool:
    """Kill a worker together with its FFmpeg/Gemini children.

    Terminating only the main.py pid leaves those grandchildren running, and
    they keep writing into the very output directory a resumed worker is about
    to use. Returns True if the tree kill was issued.
    """
    try:
        if os.name == "nt":
            # /T walks the tree, so this has to run while the parent still exists.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
            return True
        pgid = getattr(proc, "_job_pgid", None) or _process_group_id(proc)
        if pgid is None:
            return False
        os.killpg(pgid, signal.SIGKILL)
        return True
    except Exception:
        return False


def _stop_process(proc: "subprocess.Popen") -> None:
    """Stop a worker and its children, then reap it. No-op if already gone."""
    try:
        if proc.poll() is None:
            # Children first: while the parent is alive its tree is still
            # discoverable, and reaping it beforehand would strand FFmpeg.
            _kill_process_tree(proc)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        else:
            # Parent already exited — sweep whatever it left behind. Works even
            # after reaping because the group was recorded at registration.
            _kill_process_tree(proc)
    except Exception as e:
        print(f"⚠️ Failed to stop process: {e}")


def _terminate_job_processes(job_id: str) -> None:
    """Terminate (then kill) any subprocesses registered for a job. Blocking; run off the loop."""
    with job_processes_lock:
        procs = list(job_processes.get(job_id, set()))
    for proc in procs:
        _stop_process(proc)


async def _wait_for_job_processes_stopped(job_id: str, timeout_seconds: float = 20.0) -> bool:
    """Wait until run_job has reaped and unregistered every old worker."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        with job_processes_lock:
            if not job_processes.get(job_id):
                return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)


def _now_ts() -> float:
    return time.time()


def _isoformat(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or _now_ts(), tz=timezone.utc).isoformat()


def _job_state_path(job_id: str, output_dir: Optional[str] = None) -> str:
    job_dir = output_dir or os.path.join(OUTPUT_DIR, job_id)
    return os.path.join(job_dir, JOB_STATE_FILENAME)


def _job_tombstone_path(job_id: str) -> str:
    return os.path.join(JOB_TOMBSTONE_DIR, f"{job_id}.json")


def _safe_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Unique temp name per write + a lock so the log thread and the run_job
    # coroutine can't clobber the same ".tmp" file and corrupt the state.
    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    with _state_write_lock:
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _hashed_state_path(directory: str, state_id: str) -> str:
    """Return a traversal-safe state filename for an externally supplied id."""
    digest = hashlib.sha256(str(state_id).encode("utf-8")).hexdigest()
    return os.path.join(directory, f"{digest}.json")


def _remove_state_file(directory: str, state_id: str) -> None:
    path = _hashed_state_path(directory, state_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        print(f"⚠️ Failed to remove state file {path}: {e}")


def _persist_thumbnail_session(session_id: str) -> None:
    session = thumbnail_sessions.get(session_id)
    if not session:
        return
    payload = {
        key: value
        for key, value in session.items()
        if key != "transcript_event"
    }
    payload["session_id"] = session_id
    payload["updated_at"] = _now_ts()
    try:
        _safe_write_json(
            _hashed_state_path(THUMBNAIL_SESSION_STATE_DIR, session_id),
            payload,
        )
    except Exception as e:
        print(f"⚠️ Failed to persist thumbnail session {session_id}: {e}")


def _persist_publish_job(publish_id: str) -> None:
    publish_job = publish_jobs.get(publish_id)
    if not publish_job:
        return
    payload = dict(publish_job)
    payload["publish_id"] = publish_id
    payload["updated_at"] = _now_ts()
    try:
        _safe_write_json(
            _hashed_state_path(PUBLISH_JOB_STATE_DIR, publish_id),
            payload,
        )
    except Exception as e:
        print(f"⚠️ Failed to persist publish job {publish_id}: {e}")


def _persist_saas_job(job_id: str) -> None:
    # saas_jobs is declared later in the module, before application startup.
    job = globals().get("saas_jobs", {}).get(job_id)
    if not job:
        return
    output_dir = job.get("output_dir")
    if not output_dir:
        return
    payload = dict(job)
    payload["job_id"] = job_id
    payload["updated_at"] = _now_ts()
    try:
        _safe_write_json(os.path.join(output_dir, SAAS_JOB_STATE_FILENAME), payload)
    except Exception as e:
        print(f"⚠️ Failed to persist SaaS job {job_id}: {e}")


def _recover_auxiliary_state() -> None:
    """Recover thumbnail, publish and SaaS jobs after a server restart."""
    for path in glob.glob(os.path.join(THUMBNAIL_SESSION_STATE_DIR, "*.json")):
        payload = _read_json(path)
        if not payload or not payload.get("session_id"):
            continue
        session_id = str(payload.pop("session_id"))
        payload.pop("updated_at", None)
        event = asyncio.Event()
        if not payload.get("transcript_ready") and not payload.get("transcript_error"):
            payload["transcript_error"] = (
                "Server restarted before transcription completed. Please start the upload again."
            )
        event.set()
        payload["transcript_event"] = event
        thumbnail_sessions[session_id] = payload
        _persist_thumbnail_session(session_id)

    for path in glob.glob(os.path.join(PUBLISH_JOB_STATE_DIR, "*.json")):
        payload = _read_json(path)
        if not payload or not payload.get("publish_id"):
            continue
        publish_id = str(payload.pop("publish_id"))
        payload.pop("updated_at", None)
        if payload.get("status") == "uploading":
            payload["status"] = "failed"
            payload["error"] = "Server restarted before the upload status was confirmed."
        publish_jobs[publish_id] = payload
        _persist_publish_job(publish_id)

    saas_state = globals().get("saas_jobs")
    if saas_state is None:
        return
    for output_dir in glob.glob(os.path.join(OUTPUT_DIR, "saas_*")):
        if not os.path.isdir(output_dir):
            continue
        payload = _read_json(os.path.join(output_dir, SAAS_JOB_STATE_FILENAME))
        if not payload or not payload.get("job_id"):
            continue
        job_id = str(payload.pop("job_id"))
        payload.pop("updated_at", None)
        if payload.get("status") == "processing":
            payload["status"] = "failed"
            payload.setdefault("logs", []).append(
                "Server restarted during generation. Retry to reuse cached assets."
            )
        payload["output_dir"] = output_dir
        saas_state[job_id] = payload
        _persist_saas_job(job_id)


def _get_clip_operation_lock(job_id: str, clip_index: int) -> asyncio.Lock:
    key = (job_id, clip_index)
    lock = clip_operation_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        clip_operation_locks[key] = lock
    return lock


def _get_job_state_lock(job_id: str) -> asyncio.Lock:
    """Serializes read-modify-write cycles on job-wide files.

    clip_layers.json and metadata.json cover the whole job while operations
    only lock per clip, so two clips encoding in parallel would otherwise
    overwrite each other's saved state (lost update). Always acquired inside
    an already-held clip lock, never the other way around — no deadlock.
    """
    lock = job_state_locks.get(job_id)
    if lock is None:
        lock = asyncio.Lock()
        job_state_locks[job_id] = lock
    return lock


def _get_job_resume_lock(job_id: str) -> asyncio.Lock:
    """Serialize cancel, manual resume and watchdog resume for one job."""
    lock = job_resume_locks.get(job_id)
    if lock is None:
        lock = asyncio.Lock()
        job_resume_locks[job_id] = lock
    return lock


def _update_clip_version(
    job_id: str,
    clip_index: int,
    video_url: str,
    *,
    metadata_path: Optional[str] = None,
    layers: Optional[dict] = None,
) -> None:
    """Atomically persist the selected derivative in memory and metadata.

    metadata.json is always re-read from disk here: a caller-supplied snapshot
    from before a minutes-long encode would silently roll back the video_url a
    concurrent operation on another clip of the same job just persisted.
    """
    job = _get_job(job_id)
    if not job or not isinstance(job.get("result"), dict):
        raise HTTPException(status_code=400, detail="Job result not available")

    result_clips = job["result"].get("clips")
    if not isinstance(result_clips, list) or not 0 <= clip_index < len(result_clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    if metadata_path is None:
        metadata_files = glob.glob(
            os.path.join(job.get("output_dir") or os.path.join(OUTPUT_DIR, job_id), "*_metadata.json")
        )
        metadata_path = metadata_files[0] if metadata_files else None
    if not metadata_path:
        raise HTTPException(status_code=404, detail="Metadata not found")

    metadata = _read_json(metadata_path)
    metadata_clips = metadata.get("shorts") if isinstance(metadata, dict) else None
    if not isinstance(metadata_clips, list) or not 0 <= clip_index < len(metadata_clips):
        raise HTTPException(status_code=404, detail="Clip metadata not found")

    metadata_clips[clip_index]["video_url"] = video_url
    if layers is not None:
        # Surfaced to the dashboard so it can offer to remove exactly the
        # layers a clip actually carries, and keep doing so after a reload.
        metadata_clips[clip_index]["layers"] = layers
    _safe_write_json(metadata_path, metadata)
    result_clips[clip_index]["video_url"] = video_url
    if layers is not None:
        result_clips[clip_index]["layers"] = layers
    job["updated_at"] = _now_ts()
    _persist_job_state(job_id)


def _maybe_fix_mojibake_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    if not any(marker in text for marker in ("Ã", "Â", "â", "Ð", "Ñ")):
        return text
    for source_encoding in ("cp1252", "latin-1"):
        try:
            repaired = text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict")
        except Exception:
            continue
        if repaired != text:
            return repaired
    return text


def _repair_mojibake(payload):
    if isinstance(payload, str):
        return _maybe_fix_mojibake_text(payload)
    if isinstance(payload, list):
        return [_repair_mojibake(item) for item in payload]
    if isinstance(payload, dict):
        return {key: _repair_mojibake(value) for key, value in payload.items()}
    return payload


def _load_transcript_for_job(output_dir: str, metadata: Optional[dict] = None):
    transcript_files = sorted(glob.glob(os.path.join(output_dir, "*_transcript.json")))
    if transcript_files:
        transcript_payload = _read_json(transcript_files[0])
        if transcript_payload:
            return _repair_mojibake(transcript_payload)
    if metadata and metadata.get("transcript"):
        return _repair_mojibake(metadata.get("transcript"))
    return None


def _trim_list(items: List, limit: int) -> List:
    if len(items) <= limit:
        return items
    return items[-limit:]


def _make_log_entry(message: str, level: str = "info", category: str = "general", important: bool = False, ts: Optional[float] = None) -> dict:
    timestamp = ts or _now_ts()
    return {
        "timestamp": timestamp,
        "iso_timestamp": _isoformat(timestamp),
        "level": level,
        "category": category,
        "important": important,
        "message": message,
    }


def _serialize_job(job: dict) -> dict:
    persisted = {}
    allowed_keys = {
        "job_id",
        "status",
        "phase",
        "phase_label",
        "progress_percent",
        "phase_progress_percent",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
        "last_heartbeat_at",
        "elapsed_seconds",
        "actual_duration_seconds",
        "eta_seconds",
        "phase_eta_seconds",
        "total_eta_seconds",
        "eta_reference_at",
        "eta_state",
        "phase_durations_seconds",
        "worker_duration_seconds",
        "attempt",
        "resume_count",
        "auto_resume_count",
        "max_auto_resumes",
        "auto_resume_pending",
        "stall_state",
        "error_summary",
        "warnings",
        "artifacts",
        "source_type",
        "source_url",
        "input_path",
        "input_filename",
        "output_dir",
        "video_duration_seconds",
        "last_work_activity_at",
        "operation_name",
        "operation_deadline_at",
        "is_resumable",
        "raw_logs",
        "important_logs",
        "result",
        "processing_mode",
        "analysis_error",
        "analysis_status",
        "analysis_coverage",
        "archive_status",
        "job_type",
    }
    for key in allowed_keys:
        if key in job:
            persisted[key] = job[key]
    persisted["logs"] = [entry.get("message", "") for entry in persisted.get("raw_logs", [])]
    return persisted


def _persist_job_state(job_id: str) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    output_dir = job.get("output_dir")
    if not output_dir:
        return
    try:
        _safe_write_json(_job_state_path(job_id, output_dir), _serialize_job(job))
    except Exception as e:
        print(f"⚠️ Failed to persist job state for {job_id}: {e}")


def _build_job_state(job_id: str, *, output_dir: str, source_type: Optional[str] = None, source_url: Optional[str] = None,
                     input_path: Optional[str] = None, input_filename: Optional[str] = None, status: str = "queued") -> dict:
    now = _now_ts()
    return {
        "job_id": job_id,
        "job_type": "clip_generator",
        "status": status,
        "phase": "queued",
        "phase_label": "Queued",
        "progress_percent": 0.0,
        "phase_progress_percent": 0.0,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "updated_at": now,
        "last_heartbeat_at": now,
        "elapsed_seconds": 0.0,
        "actual_duration_seconds": None,
        "eta_seconds": None,
        "phase_eta_seconds": None,
        "total_eta_seconds": None,
        "eta_reference_at": None,
        "eta_state": "calculating",
        "phase_durations_seconds": {},
        "attempt": 0,
        "resume_count": 0,
        "auto_resume_count": 0,
        "max_auto_resumes": MAX_AUTO_RESUMES,
        "auto_resume_pending": False,
        "stall_state": "healthy",
        "error_summary": None,
        "warnings": [],
        "artifacts": {},
        "source_type": source_type,
        "source_url": source_url,
        "input_path": input_path,
        "input_filename": input_filename,
        "output_dir": output_dir,
        "video_duration_seconds": None,
        "last_work_activity_at": now,
        "operation_name": None,
        "operation_deadline_at": None,
        "is_resumable": False,
        "raw_logs": [],
        "important_logs": [],
        "logs": [],
        "result": None,
        "analysis_status": None,
        "analysis_error": None,
        "analysis_coverage": None,
        "processing_mode": None,
    }


def _append_log(job_id: str, message: str, *, level: str = "info", category: str = "general", important: bool = False, ts: Optional[float] = None) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    entry = _make_log_entry(message, level=level, category=category, important=important, ts=ts)
    job.setdefault("raw_logs", []).append(entry)
    job["raw_logs"] = _trim_list(job["raw_logs"], JOB_LOG_LIMIT)
    if important:
        job.setdefault("important_logs", []).append(entry)
        job["important_logs"] = _trim_list(job["important_logs"], IMPORTANT_LOG_LIMIT)
    job["logs"] = [log_entry["message"] for log_entry in job["raw_logs"]]
    job["updated_at"] = entry["timestamp"]
    _persist_job_state(job_id)


def _set_job_result(job_id: str, result: dict) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    job["result"] = result
    job["updated_at"] = _now_ts()
    _persist_job_state(job_id)


def _mark_job_status(job_id: str, status: str, *, error_summary: Optional[str] = None, resumable: Optional[bool] = None) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    now = _now_ts()
    job["status"] = status
    job["updated_at"] = now
    if status == "processing" and not job.get("started_at"):
        job["started_at"] = now
    if status == "processing":
        job["finished_at"] = None
        job["actual_duration_seconds"] = None
        job["eta_state"] = "calculating"
        job["auto_resume_pending"] = False
    elif status in TERMINAL_JOB_STATUSES:
        finished_at = float(job.get("finished_at") or now)
        job["finished_at"] = finished_at
        started_at = job.get("started_at")
        job["actual_duration_seconds"] = (
            max(0, int(finished_at - float(started_at))) if started_at else None
        )
        job["phase_eta_seconds"] = 0
        job["eta_seconds"] = 0
        job["total_eta_seconds"] = 0
        job["eta_state"] = "done"
        job["auto_resume_pending"] = False
        job["operation_name"] = None
        job["operation_deadline_at"] = None
    if error_summary is not None:
        job["error_summary"] = error_summary
    if resumable is not None:
        job["is_resumable"] = resumable
    _persist_job_state(job_id)


def _event_log_defaults(event_type: str) -> tuple[str, bool]:
    if event_type in {"error"}:
        return "error", True
    if event_type in {"warning", "slow", "timeout", "stalled"}:
        return "warning", True
    if event_type in {"phase", "artifact", "resume", "summary"}:
        return "info", True
    return "info", False


def _apply_job_event(job_id: str, event: dict) -> None:
    job = jobs.get(job_id)
    if not job:
        return

    now = float(event.get("timestamp") or _now_ts())
    event_type = str(event.get("type") or "log")
    message = str(event.get("message") or "").strip()
    category = str(event.get("category") or event.get("phase") or "general")
    level, default_important = _event_log_defaults(event_type)
    important = bool(event.get("important", default_important))

    job["updated_at"] = now
    if event_type in {"heartbeat", "phase", "progress", "slow", "resume"}:
        job["last_heartbeat_at"] = now
    if event_type == "stalled":
        job["stall_state"] = "stalled"
        job["status"] = "stalled"
        job["is_resumable"] = True
    elif event_type == "slow":
        job["stall_state"] = "slow"
    elif event_type in {"heartbeat", "progress", "phase", "resume"}:
        # The log thread keeps draining for up to 5s after a stalled worker was
        # killed. A buffered event from that window must not report the job as
        # healthy again while its status still says stalled.
        if job.get("status") != "stalled" and not job.get("stall_termination_requested"):
            job["stall_state"] = "healthy"

    event_status = event.get("status")
    # A worker's final summary is advisory. The supervising process validates
    # metadata/video artifacts before exposing "completed" to the frontend.
    if event_status and not (event_type == "summary" and event_status == "completed"):
        job["status"] = event_status
    if event.get("phase"):
        job["phase"] = event["phase"]
    if event.get("phase_label"):
        job["phase_label"] = event["phase_label"]
    if event.get("progress_percent") is not None:
        job["progress_percent"] = max(0.0, min(100.0, float(event["progress_percent"])))
    if event.get("phase_progress_percent") is not None:
        job["phase_progress_percent"] = max(0.0, min(100.0, float(event["phase_progress_percent"])))
    if "phase_eta_seconds" in event:
        value = event.get("phase_eta_seconds")
        job["phase_eta_seconds"] = None if value is None else max(0, int(value))
        # Anchor the countdown so the status endpoint can age the estimate
        # between events instead of reporting a frozen number.
        job["eta_reference_at"] = now
    if "total_eta_seconds" in event:
        value = event.get("total_eta_seconds")
        job["total_eta_seconds"] = None if value is None else max(0, int(value))
    if "eta_seconds" in event:
        value = event.get("eta_seconds")
        job["eta_seconds"] = None if value is None else max(0, int(value))
    if event.get("eta_state") in {"calculating", "estimated", "live", "done"}:
        job["eta_state"] = event["eta_state"]
    if isinstance(event.get("phase_durations_seconds"), dict):
        job["phase_durations_seconds"] = {
            str(key): max(0.0, float(value))
            for key, value in event["phase_durations_seconds"].items()
            if isinstance(value, (int, float))
        }
    if event.get("worker_duration_seconds") is not None:
        job["worker_duration_seconds"] = max(0.0, float(event["worker_duration_seconds"]))
    if event.get("attempt") is not None:
        job["attempt"] = int(event["attempt"])
    if event.get("resume_count") is not None:
        job["resume_count"] = int(event["resume_count"])
    if event.get("video_duration_seconds") is not None:
        job["video_duration_seconds"] = float(event["video_duration_seconds"])
    if event.get("work_activity_at") is not None:
        job["last_work_activity_at"] = float(event["work_activity_at"])
    if "operation_name" in event:
        job["operation_name"] = event.get("operation_name")
    if "operation_deadline_at" in event:
        value = event.get("operation_deadline_at")
        job["operation_deadline_at"] = None if value is None else float(value)
    if event.get("resumable") is not None:
        job["is_resumable"] = bool(event["resumable"])
    if event.get("processing_mode") is not None:
        job["processing_mode"] = event["processing_mode"]
    if event.get("analysis_status") is not None:
        job["analysis_status"] = event["analysis_status"]
    if event.get("analysis_error") is not None:
        job["analysis_error"] = event["analysis_error"]
        job["error_summary"] = event["analysis_error"]
    if isinstance(event.get("analysis_coverage"), dict):
        job["analysis_coverage"] = event["analysis_coverage"]

    artifact = event.get("artifact")
    if isinstance(artifact, dict):
        kind = artifact.get("kind")
        path = artifact.get("path")
        if kind and path:
            job.setdefault("artifacts", {})[kind] = path

    if event_type in {"warning", "slow", "timeout"} and message:
        warnings = job.setdefault("warnings", [])
        warnings.append(message)
        job["warnings"] = _trim_list(warnings, 100)

    if event_type == "error" and message:
        job["error_summary"] = message
        job["status"] = "failed"
        job["is_resumable"] = True

    if event_type == "summary" and event.get("status") == "completed":
        job["worker_summary_received"] = True

    if message:
        _append_log(job_id, message, level=level, category=category, important=important, ts=now)
    else:
        _persist_job_state(job_id)


def _classify_raw_log(message: str) -> tuple[str, str, bool]:
    stripped = message.strip()
    lower = stripped.lower()

    if re.match(r"^\s*\[\d+(\.\d+)?s\s*->\s*\d+(\.\d+)?s\]", stripped):
        return "info", "transcript", False
    if "processing:" in lower or "tqdm" in lower:
        return "info", "progress", False

    # Treat yt-dlp/debug output as low-signal unless it clearly contains a fatal marker.
    if stripped.startswith("[debug]"):
        if any(token in lower for token in ("traceback", "fatal", "exception")):
            return "error", "error", True
        return "info", "debug", False

    if stripped.startswith("[download]") and "%" in stripped:
        return "info", "progress", False
    if stripped.startswith("[download] Destination:") or "destination:" in lower:
        return "info", "download", True

    if stripped.startswith("WARNING:") or "⚠️" in stripped:
        return "warning", "warning", True
    if (
        stripped.startswith(("ERROR:", "Error:", "FATAL:", "Fatal:", "Traceback"))
        or "❌" in stripped
        or re.search(r"\b(exception|traceback|fatal)\b", lower)
    ):
        return "error", "error", True
    if any(token in stripped for token in ("✅", "🔥", "📝 Saved", "⏱️", "🔄", "🤖", "🎬", "🧹", "⏩")):
        return "info", "system", True
    return "info", "raw", False


def _display_log_entry(entry: dict) -> dict:
    message = entry.get("message", "")
    stripped = message.strip()
    looks_like_worker_output = (
        stripped.startswith("[")
        or stripped.startswith(("WARNING:", "ERROR:", "Error:", "FATAL:", "Fatal:", "Traceback"))
        or any(token in stripped for token in ("✅", "🔥", "📝", "⏱️", "🔄", "🤖", "🎬", "🧹", "⏩", "❌", "⚠️"))
    )
    if not looks_like_worker_output:
        return entry

    level, category, important = _classify_raw_log(message)
    normalized = dict(entry)
    normalized["level"] = level
    normalized["category"] = category
    normalized["important"] = important
    return normalized


def _hydrate_job_from_disk(job_id: str) -> Optional[dict]:
    path = _job_state_path(job_id)
    payload = _read_json(path)
    if not payload:
        return None
    payload.setdefault("job_id", job_id)
    payload.setdefault("output_dir", os.path.join(OUTPUT_DIR, job_id))
    payload.setdefault("raw_logs", [])
    payload.setdefault("important_logs", [])
    payload["logs"] = [entry.get("message", "") for entry in payload.get("raw_logs", [])]
    jobs[job_id] = payload
    return payload


def _get_job(job_id: str) -> Optional[dict]:
    return jobs.get(job_id) or _hydrate_job_from_disk(job_id)


def _build_archive_payload(job: dict) -> dict:
    return {
        "job_id": job.get("job_id"),
        "status": "archived",
        "archived_at": _isoformat(),
        "last_status": job.get("status"),
        "error_summary": job.get("error_summary"),
        "source_type": job.get("source_type"),
        "source_url": job.get("source_url"),
        "phase": job.get("phase"),
        "progress_percent": job.get("progress_percent"),
        "video_duration_seconds": job.get("video_duration_seconds"),
        "is_resumable": False,
    }


def _write_tombstone(job_id: str, job: dict) -> None:
    _safe_write_json(_job_tombstone_path(job_id), _build_archive_payload(job))


def _get_job_tombstone(job_id: str) -> Optional[dict]:
    return _read_json(_job_tombstone_path(job_id))


def _recover_jobs_from_disk() -> None:
    for entry in os.listdir(OUTPUT_DIR):
        if entry.startswith(".") or entry == "thumbnails":
            continue
        output_dir = os.path.join(OUTPUT_DIR, entry)
        if not os.path.isdir(output_dir):
            continue
        state = _read_json(_job_state_path(entry, output_dir))
        if not state:
            continue
        state.setdefault("job_id", entry)
        state.setdefault("output_dir", output_dir)
        state.setdefault("raw_logs", [])
        state.setdefault("important_logs", [])
        state["logs"] = [log_entry.get("message", "") for log_entry in state.get("raw_logs", [])]
        # Scheduled asyncio tasks do not survive an API restart. Never restore a
        # persisted "pending" badge without a live task/token behind it.
        state["auto_resume_pending"] = False
        state.pop("auto_resume_token", None)
        if state.get("status") in ACTIVE_JOB_STATUSES:
            state["status"] = "stalled"
            state["stall_state"] = "stalled"
            state["is_resumable"] = True
            state["error_summary"] = state.get("error_summary") or "Server restart detected while the job was still running."
            state["updated_at"] = _now_ts()
            state["last_heartbeat_at"] = state.get("last_heartbeat_at") or state["updated_at"]
            jobs[entry] = state
            _append_log(entry, "Server restart detected. Job marked as stalled and resumable.", category="resume", important=True)
        else:
            jobs[entry] = state

def _relocate_root_job_artifacts(job_id: str, job_output_dir: str) -> bool:
    """
    Backward-compat rescue:
    If main.py accidentally wrote metadata/clips into OUTPUT_DIR root (e.g. output/<jobid>_...),
    move them into output/<job_id>/ so the API can find and serve them.
    """
    try:
        os.makedirs(job_output_dir, exist_ok=True)
        root = OUTPUT_DIR
        pattern = os.path.join(root, f"{job_id}_*_metadata.json")
        meta_candidates = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)
        if not meta_candidates:
            return False

        # Move the newest metadata and its associated clips.
        metadata_path = meta_candidates[0]
        base_name = os.path.basename(metadata_path).replace("_metadata.json", "")

        # Move metadata
        dest_metadata = os.path.join(job_output_dir, os.path.basename(metadata_path))
        if os.path.abspath(metadata_path) != os.path.abspath(dest_metadata):
            shutil.move(metadata_path, dest_metadata)

        # Move any clips that match the same base_name into the job folder
        clip_pattern = os.path.join(root, f"{base_name}_clip_*.mp4")
        for clip_path in glob.glob(clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        # Also move any temp_ clips that might remain
        temp_clip_pattern = os.path.join(root, f"temp_{base_name}_clip_*.mp4")
        for clip_path in glob.glob(temp_clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        return True
    except Exception:
        return False


def _resolve_clip_filename(clip: dict, base_name: str, clip_index: int) -> str:
    video_url = clip.get("video_url")
    if video_url:
        filename = os.path.basename(video_url)
        if filename:
            return filename

    output_filename = clip.get("output_filename")
    if output_filename:
        return os.path.basename(output_filename)

    return f"{base_name}_clip_{clip_index + 1}.mp4"


def _build_result_from_metadata(job_id: str, metadata_path: str, output_dir: str, ready_only: bool = False) -> Optional[dict]:
    with open(metadata_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    base_name = os.path.basename(metadata_path).replace('_metadata.json', '')
    clips = []

    for i, clip in enumerate(data.get('shorts', [])):
        if not isinstance(clip, dict):
            continue

        clip_filename = _resolve_clip_filename(clip, base_name, i)
        clip_path = os.path.join(output_dir, clip_filename)
        if not os.path.exists(clip_path) or os.path.getsize(clip_path) <= 0:
            continue

        clip_data = dict(clip)
        clip_data['output_filename'] = clip_filename
        clip_data['video_url'] = f"/videos/{job_id}/{clip_filename}"
        clips.append(clip_data)

    if not clips:
        return None

    result = {'clips': clips, 'cost_analysis': data.get('cost_analysis')}
    for extra_key in ('analysis_status', 'analysis_error', 'analysis_coverage', 'processing_mode'):
        if extra_key in data:
            result[extra_key] = data.get(extra_key)

    return result


def _build_result_from_video_artifacts(job_id: str, output_dir: str) -> Optional[dict]:
    fallback_candidates = sorted(
        {
            path
            for output_format in CANONICAL_OUTPUT_FORMATS
            for path in glob.glob(os.path.join(output_dir, f"*_{output_format}.mp4"))
        },
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    if not fallback_candidates:
        return None

    fallback_path = fallback_candidates[0]
    fallback_filename = os.path.basename(fallback_path)
    fallback_stem = os.path.splitext(fallback_filename)[0]
    fallback_stem = re.sub(r"_(?:vertical|square|original)$", "", fallback_stem)
    fallback_title = fallback_stem.replace("_", " ").strip()

    return {
        'clips': [
            {
                'start': 0.0,
                'end': 0.0,
                'video_title_for_youtube_short': fallback_title or "Fallback video",
                'video_description_for_tiktok': "Automatic fallback output generated without clip metadata.",
                'video_description_for_instagram': "Automatic fallback output generated without clip metadata.",
                'viral_hook_text': "Automatic fallback",
                'output_filename': fallback_filename,
                'video_url': f"/videos/{job_id}/{fallback_filename}",
            }
        ],
        'analysis_status': 'fallback_missing_metadata',
        'analysis_error': 'No metadata file was generated, but a fallback video was created successfully.',
        'processing_mode': 'full_video_fallback',
    }

async def _auto_resume_after_stall(job_id: str, attempt: int, token: str):
    """Restart the exact stalled generation represented by ``token``."""
    try:
        backoff = AUTO_RESUME_BACKOFF_SECONDS[min(attempt - 1, len(AUTO_RESUME_BACKOFF_SECONDS) - 1)]
        await asyncio.sleep(backoff)
        await _resume_job_internal(
            job_id,
            auto_token=token,
            reason=f"Automatic restart {attempt} after a detected freeze.",
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"⚠️ Auto-resume for {job_id} failed: {e}")
        async with _get_job_resume_lock(job_id):
            job = jobs.get(job_id)
            if job and job.get("auto_resume_token") == token:
                job["auto_resume_pending"] = False
                job.pop("auto_resume_token", None)
                _persist_job_state(job_id)
        _append_log(job_id, f"Automatic restart failed: {e}", level="error", category="stall", important=True)


def _job_stall_reason(job: dict, now: float) -> Optional[str]:
    """Return why a processing job must be stopped, even if it still heartbeats."""
    operation_deadline = job.get("operation_deadline_at")
    if operation_deadline is not None and now >= float(operation_deadline):
        operation_name = job.get("operation_name") or "blocking operation"
        return f"Operation '{operation_name}' exceeded its activity deadline."

    last_heartbeat_at = (
        job.get("last_heartbeat_at")
        or job.get("updated_at")
        or job.get("created_at")
        or now
    )
    heartbeat_age = now - float(last_heartbeat_at)
    if heartbeat_age >= HEARTBEAT_STALLED_SECONDS:
        return f"No heartbeat received for {int(heartbeat_age)}s."
    return None


async def heartbeat_monitor():
    """Flag jobs whose worker went silent, kill them and restart them.

    This used to live inside cleanup_jobs' 5-minute loop, which meant the
    threshold was checked far too late to be meaningful.
    """
    print("💓 Heartbeat monitor started.")
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_MONITOR_INTERVAL_SECONDS)
            now = time.time()
            stalled_job_ids = []

            for job_id, job in list(jobs.items()):
                if job.get("status") != "processing":
                    continue
                last_heartbeat_at = job.get("last_heartbeat_at") or job.get("updated_at") or job.get("created_at") or now
                heartbeat_age = now - float(last_heartbeat_at)
                stall_reason = _job_stall_reason(job, now)
                if stall_reason:
                    job["status"] = "stalled"
                    job["stall_state"] = "stalled"
                    job["stall_termination_requested"] = True
                    job["is_resumable"] = True
                    job["auto_resume_pending"] = False
                    job["error_summary"] = stall_reason
                    _append_log(
                        job_id,
                        f"{stall_reason} Stopping the old process before resume.",
                        level="warning", category="stall", important=True,
                    )
                    stalled_job_ids.append(job_id)
                elif heartbeat_age >= HEARTBEAT_STALL_WARNING_SECONDS and job.get("stall_state") != "slow":
                    job["stall_state"] = "slow"
                    _append_log(job_id, "Job is slower than expected but still waiting for activity.",
                                level="warning", category="stall", important=True)

            # Never let a stalled worker keep its semaphore slot or write into
            # the same output directory as a resumed worker.
            for stalled_job_id in stalled_job_ids:
                await asyncio.get_running_loop().run_in_executor(
                    None, _terminate_job_processes, stalled_job_id
                )
                job = jobs.get(stalled_job_id) or {}
                # A manual resume/cancel may have won while taskkill was running.
                if job.get("status") != "stalled":
                    continue
                budget = int(job.get("max_auto_resumes", MAX_AUTO_RESUMES))
                used = int(job.get("auto_resume_count") or 0)
                if used >= budget:
                    job["auto_resume_pending"] = False
                    job.pop("auto_resume_token", None)
                    _append_log(
                        stalled_job_id,
                        f"Automatic restarts exhausted ({used}/{budget}). Please resume manually.",
                        level="warning", category="stall", important=True,
                    )
                    continue
                token = uuid.uuid4().hex
                job["auto_resume_count"] = used + 1
                job["auto_resume_pending"] = True
                job["auto_resume_token"] = token
                _append_log(
                    stalled_job_id,
                    f"Automatic restart {used + 1}/{budget} scheduled after the detected freeze.",
                    level="warning", category="stall", important=True,
                )
                asyncio.create_task(_auto_resume_after_stall(stalled_job_id, used + 1, token))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️ Heartbeat monitor error: {e}")


async def cleanup_jobs():
    """Background task to remove old jobs and files."""
    print("🧹 Cleanup task started.")
    while True:
        try:
            await asyncio.sleep(300) # Check every 5 minutes
            now = time.time()

            protected_uploads = set()
            for job_id, job in list(jobs.items()):
                status = job.get("status")
                input_path = job.get("input_path")
                if input_path and (status in ACTIVE_JOB_STATUSES or job.get("is_resumable")):
                    protected_uploads.add(os.path.abspath(input_path))

            for job_id in os.listdir(OUTPUT_DIR):
                if job_id.startswith(".") or job_id == "thumbnails":
                    continue
                job_path = os.path.join(OUTPUT_DIR, job_id)
                if not os.path.isdir(job_path):
                    continue

                job = _get_job(job_id)
                if job and job.get("status") in ACTIVE_JOB_STATUSES:
                    continue

                state_path = _job_state_path(job_id, job_path)
                if os.path.exists(state_path):
                    state = _read_json(state_path) or {}
                    status = state.get("status")
                    if status in ACTIVE_JOB_STATUSES:
                        continue

                try:
                    mtime = os.path.getmtime(job_path)
                except FileNotFoundError:
                    continue

                if now - mtime <= JOB_RETENTION_SECONDS:
                    continue

                archived_job = job or _read_json(state_path) or {"job_id": job_id, "status": "archived"}
                print(f"🧹 Purging old job: {job_id}")
                try:
                    _write_tombstone(job_id, archived_job)
                except Exception as e:
                    print(f"⚠️ Failed to write tombstone for {job_id}: {e}")
                shutil.rmtree(job_path, ignore_errors=True)
                jobs.pop(job_id, None)
                for lock_key in [key for key in clip_operation_locks if key[0] == job_id]:
                    clip_operation_locks.pop(lock_key, None)
                job_state_locks.pop(job_id, None)
                job_resume_locks.pop(job_id, None)

            # Cleanup SaaSShorts jobs from memory
            try:
                saas_expired = [
                    jid for jid, jdata in list(saas_jobs.items())
                    if jdata.get("status") in ("completed", "failed")
                    and jdata.get("output_dir")
                    and os.path.isdir(jdata["output_dir"])
                    and now - os.path.getmtime(jdata["output_dir"]) > JOB_RETENTION_SECONDS
                ]
                for jid in saas_expired:
                    del saas_jobs[jid]
            except NameError:
                pass

            # Cleanup thumbnail sessions (TTL-based; created_at added at creation).
            for sid, session in list(thumbnail_sessions.items()):
                created = session.get("created_at") or 0
                if now - float(created) > THUMBNAIL_SESSION_TTL_SECONDS:
                    thumbnail_sessions.pop(sid, None)
                    _remove_state_file(THUMBNAIL_SESSION_STATE_DIR, sid)

            # Cleanup finished publish jobs (TTL-based).
            for pid, pjob in list(publish_jobs.items()):
                created = pjob.get("created_at") or 0
                if now - float(created) > PUBLISH_JOB_TTL_SECONDS:
                    publish_jobs.pop(pid, None)
                    _remove_state_file(PUBLISH_JOB_STATE_DIR, pid)

            # Cleanup Uploads
            for filename in os.listdir(UPLOAD_DIR):
                file_path = os.path.join(UPLOAD_DIR, filename)
                try:
                    if os.path.abspath(file_path) in protected_uploads:
                        continue
                    if now - os.path.getmtime(file_path) > JOB_RETENTION_SECONDS:
                        os.remove(file_path)
                except Exception: pass

        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")

async def process_queue():
    """Background worker to process jobs from the queue with concurrency limit."""
    print(f"🚀 Job Queue Worker started with {MAX_CONCURRENT_JOBS} concurrent slots.")
    while True:
        try:
            # Wait for a job
            job_id = await job_queue.get()
            
            # Acquire semaphore slot (waits if max jobs are running)
            await concurrency_semaphore.acquire()
            print(f"🔄 Acquired slot for job: {job_id}")

            # Process in background task to not block the loop (allowing other slots to fill)
            asyncio.create_task(run_job_wrapper(job_id))
            
        except Exception as e:
            print(f"❌ Queue dispatch error: {e}")
            await asyncio.sleep(1)

async def run_job_wrapper(job_id):
    """Wrapper to run job and release semaphore"""
    try:
        job = jobs.get(job_id)
        if job:
            await run_job(job_id, job)
    except Exception as e:
         print(f"❌ Job wrapper error {job_id}: {e}")
    finally:
        # Always release semaphore and mark queue task done
        concurrency_semaphore.release()
        job_queue.task_done()
        print(f"✅ Released slot for job: {job_id}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start worker and cleanup
    _recover_jobs_from_disk()
    _recover_auxiliary_state()
    worker_task = asyncio.create_task(process_queue())
    cleanup_task = asyncio.create_task(cleanup_jobs())
    heartbeat_task = asyncio.create_task(heartbeat_monitor())
    yield
    # Cleanup (optional: cancel worker)

app = FastAPI(lifespan=lifespan)

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compress JSON responses: a real 249 KB status-poll payload measures 29.6 KB
# gzipped (-88%). Video files are served via /videos (already compressed
# codecs, > minimum_size guard not relevant since StaticFiles streams).
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Mount static files for serving videos
app.mount("/videos", StaticFiles(directory=OUTPUT_DIR), name="videos")

# Mount static files for serving thumbnails
THUMBNAILS_DIR = os.path.join(OUTPUT_DIR, "thumbnails")
os.makedirs(THUMBNAILS_DIR, exist_ok=True)
app.mount("/thumbnails", StaticFiles(directory=THUMBNAILS_DIR), name="thumbnails")

class ProcessRequest(BaseModel):
    url: str


class ResumeRequest(BaseModel):
    phase: Optional[str] = None


def _build_resume_command(job_id: str, output_dir: str, phase: Optional[str] = None) -> List[str]:
    cmd = [sys.executable, "-u", "main.py", "--resume-dir", output_dir, "--job-id", job_id]
    if phase:
        cmd.extend(["--resume-phase", phase])
    return cmd

def enqueue_output(out, job_id):
    """Reads output from a subprocess and appends it to jobs logs."""
    try:
        for line in iter(out.readline, b''):
            decoded = line.decode('utf-8', errors='replace')
            # yt-dlp writes download progress with bare carriage returns (no
            # newline) into the same stdout as our __JOB_EVENT__ lines. Without
            # splitting on \r, heartbeat events get glued behind progress
            # fragments, are never recognized, and the stall monitor kills a
            # perfectly healthy download after HEARTBEAT_STALLED_SECONDS.
            for segment in decoded.replace('\r', '\n').split('\n'):
                decoded_line = segment.strip()
                if not decoded_line:
                    continue
                print(f"📝 [Job Output] {decoded_line}")
                if job_id not in jobs:
                    continue
                event_idx = decoded_line.find(EVENT_PREFIX)
                if event_idx != -1:
                    try:
                        event = json.loads(decoded_line[event_idx + len(EVENT_PREFIX):].strip())
                        _apply_job_event(job_id, event)
                        continue
                    except Exception as e:
                        _append_log(job_id, f"Failed to parse worker event: {e}", level="warning", category="event", important=True)
                level, category, important = _classify_raw_log(decoded_line)
                _append_log(job_id, decoded_line, level=level, category=category, important=important)
    except Exception as e:
        print(f"Error reading output for job {job_id}: {e}")
    finally:
        out.close()


def _refresh_job_result(job_id: str, output_dir: str) -> Optional[dict]:
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        if _relocate_root_job_artifacts(job_id, output_dir):
            json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    result = None
    if json_files:
        result = _build_result_from_metadata(
            job_id=job_id,
            metadata_path=json_files[0],
            output_dir=output_dir,
            ready_only=False,
        )
    if not result:
        result = _build_result_from_video_artifacts(job_id, output_dir)
    if result:
        _set_job_result(job_id, result)
    return result


def _build_status_payload(job: dict) -> dict:
    now = _now_ts()
    started_at = job.get("started_at")
    finished_at = job.get("finished_at")
    if not finished_at and job.get("status") in TERMINAL_JOB_STATUSES:
        # Backward-compatible freeze for jobs completed before finished_at was
        # persisted. updated_at is the supervisor's last validated job write.
        finished_at = job.get("updated_at") or job.get("last_heartbeat_at")
    last_heartbeat_at = job.get("last_heartbeat_at")
    elapsed_seconds = None
    if started_at:
        elapsed_end = float(finished_at) if finished_at else now
        elapsed_seconds = max(0, int(elapsed_end - float(started_at)))
    actual_duration_seconds = job.get("actual_duration_seconds")
    if actual_duration_seconds is None and finished_at and started_at:
        actual_duration_seconds = elapsed_seconds
    seconds_since_heartbeat = None
    if last_heartbeat_at:
        seconds_since_heartbeat = max(0, int(now - float(last_heartbeat_at)))
    seconds_since_finish = None
    if finished_at:
        seconds_since_finish = max(0, int(now - float(finished_at)))

    # Age the ETAs by the time since the worker last reported one. Without this
    # the countdown freezes for as long as a phase stays silent and then jumps.
    eta_state = job.get("eta_state") or "calculating"
    eta_age = 0.0
    if job.get("eta_reference_at") and eta_state != "done":
        eta_age = max(0.0, now - float(job["eta_reference_at"]))

    def _aged(value):
        if value is None:
            return None
        return max(0, int(round(float(value) - eta_age)))

    phase_eta_seconds = _aged(job.get("phase_eta_seconds"))
    total_eta_seconds = _aged(job.get("total_eta_seconds"))

    display_raw_logs = [_display_log_entry(entry) for entry in job.get("raw_logs", [])][-JOB_LOG_LIMIT:]
    display_important_logs = [entry for entry in display_raw_logs if entry.get("important")][-IMPORTANT_LOG_LIMIT:]

    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "phase_label": job.get("phase_label"),
        "progress_percent": job.get("progress_percent"),
        "phase_progress_percent": job.get("phase_progress_percent"),
        "eta_seconds": phase_eta_seconds,
        "phase_eta_seconds": phase_eta_seconds,
        "total_eta_seconds": total_eta_seconds,
        "eta_state": eta_state,
        "elapsed_seconds": elapsed_seconds,
        "actual_duration_seconds": actual_duration_seconds,
        "created_at": job.get("created_at"),
        "finished_at": finished_at,
        "updated_at": job.get("updated_at"),
        "last_heartbeat_at": last_heartbeat_at,
        "seconds_since_heartbeat": seconds_since_heartbeat,
        "seconds_since_finish": seconds_since_finish,
        "attempt": job.get("attempt"),
        "resume_count": job.get("resume_count"),
        "auto_resume_count": job.get("auto_resume_count", 0),
        "max_auto_resumes": job.get("max_auto_resumes", MAX_AUTO_RESUMES),
        "auto_resume_pending": bool(job.get("auto_resume_pending", False)),
        "stall_state": job.get("stall_state"),
        "error_summary": job.get("error_summary"),
        "warnings": job.get("warnings", []),
        "is_resumable": job.get("is_resumable", False),
        "source_type": job.get("source_type"),
        "source_url": job.get("source_url"),
        "video_duration_seconds": job.get("video_duration_seconds"),
        "last_work_activity_at": job.get("last_work_activity_at"),
        "operation_name": job.get("operation_name"),
        "operation_deadline_at": job.get("operation_deadline_at"),
        "phase_durations_seconds": job.get("phase_durations_seconds", {}),
        "worker_duration_seconds": job.get("worker_duration_seconds"),
        "important_logs": display_important_logs,
        "raw_logs": display_raw_logs,
        "logs": [entry.get("message", "") for entry in display_raw_logs],
        "result": job.get("result"),
        "analysis_status": job.get("analysis_status"),
        "analysis_error": job.get("analysis_error"),
        "analysis_coverage": job.get("analysis_coverage"),
        "processing_mode": job.get("processing_mode"),
        "artifacts": job.get("artifacts", {}),
    }


async def run_job(job_id, job_data):
    """Executes the subprocess for a specific job."""

    cmd = job_data['cmd']
    env = job_data['env']
    output_dir = job_data['output_dir']
    execution_id = job_data.get("execution_id")

    _mark_job_status(job_id, 'processing', resumable=True)
    jobs[job_id]['phase'] = 'queued'
    jobs[job_id]['phase_label'] = 'Queued'
    jobs[job_id]['last_heartbeat_at'] = _now_ts()
    jobs[job_id]['updated_at'] = jobs[job_id]['last_heartbeat_at']
    _append_log(job_id, "Job started by worker.", category="queue", important=True)
    print(f"🎬 [run_job] Executing command for {job_id}: {' '.join(cmd)}")

    # If the job was cancelled while still queued, don't spawn anything.
    if jobs.get(job_id, {}).get("cancel_requested"):
        _mark_job_status(job_id, 'failed', error_summary="Cancelled by user", resumable=False)
        _append_log(job_id, "Job cancelled before start.", level="warning", category="cancel", important=True)
        return

    process = None
    t_log = None
    try:
        # Own process group/session so _stop_process can take the worker's
        # FFmpeg and Gemini children down with it instead of orphaning them.
        if os.name == "nt":
            spawn_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            spawn_kwargs = {"start_new_session": True}
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr to stdout
            env=env,
            cwd=os.getcwd(),
            **spawn_kwargs,
        )
        _register_job_process(job_id, process)

        # We need to capture logs in a thread because Popen isn't async
        t_log = threading.Thread(target=enqueue_output, args=(process.stdout, job_id))
        t_log.daemon = True
        t_log.start()

        # Async wait for process with incremental updates
        start_wait = time.time()
        while process.poll() is None:
            await asyncio.sleep(2)

            # Stop promptly if the job was cancelled via the cancel endpoint.
            if jobs.get(job_id, {}).get("cancel_requested"):
                break

            # Check for partial results every 2 seconds
            # Look for metadata file
            try:
                json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
                if json_files:
                    target_json = json_files[0]
                    if os.path.getsize(target_json) > 0:
                        partial_result = _build_result_from_metadata(
                            job_id=job_id,
                            metadata_path=target_json,
                            output_dir=output_dir,
                            ready_only=True,
                        )
                        if partial_result:
                            jobs[job_id]['result'] = partial_result
            except Exception as e:
                # Ignore read errors during processing
                pass

        # A resume request installs a new execution id only after stopping this
        # process. The old supervisor must then exit without overwriting the new
        # queued state.
        if jobs.get(job_id, {}).get("execution_id") != execution_id:
            return

        # Cancellation path: kill the worker and mark the job accordingly.
        if jobs.get(job_id, {}).get("cancel_requested"):
            _stop_process(process)
            if t_log:
                await asyncio.get_running_loop().run_in_executor(None, t_log.join, 5)
            _mark_job_status(job_id, 'failed', error_summary="Cancelled by user", resumable=False)
            _append_log(job_id, "Job cancelled by user.", level="warning", category="cancel", important=True)
            return

        # The heartbeat monitor stopped this exact process. Keep the resumable
        # stalled state instead of converting the termination into a generic
        # exit-code failure.
        if jobs.get(job_id, {}).get("stall_termination_requested"):
            _stop_process(process)
            if t_log:
                await asyncio.get_running_loop().run_in_executor(None, t_log.join, 5)
            jobs[job_id].pop("stall_termination_requested", None)
            _persist_job_state(job_id)
            _append_log(
                job_id,
                "Old stalled process stopped. The job can now be resumed safely.",
                level="warning",
                category="stall",
                important=True,
            )
            return

        # Drain the worker's final structured events before deciding the public
        # status. Otherwise a late error/summary can race with completion.
        if t_log:
            await asyncio.get_running_loop().run_in_executor(None, t_log.join, 5)

        returncode = process.returncode

        if returncode == 0:
            final_result = _refresh_job_result(job_id, output_dir)
            if final_result:
                if final_result.get("analysis_status"):
                    jobs[job_id]["analysis_status"] = final_result.get("analysis_status")
                if final_result.get("analysis_error"):
                    jobs[job_id]["analysis_error"] = final_result.get("analysis_error")
                if final_result.get("processing_mode"):
                    jobs[job_id]["processing_mode"] = final_result.get("processing_mode")
                if final_result.get("processing_mode") == "full_video_fallback":
                    _append_log(job_id, "Metadata missing, but fallback video artifacts were recovered.", level="warning", category="fallback", important=True)
                _mark_job_status(job_id, 'completed', resumable=False)
                _append_log(job_id, "Process finished successfully.", category="summary", important=True)

                # Start S3 upload only after local result validation succeeded.
                loop = asyncio.get_event_loop()
                loop.run_in_executor(None, upload_job_artifacts, output_dir, job_id)
            else:
                _mark_job_status(job_id, 'failed', error_summary="No metadata file generated.", resumable=True)
                _append_log(job_id, "No metadata file generated.", level="error", category="result", important=True)
        else:
            _mark_job_status(job_id, 'failed', error_summary=f"Process failed with exit code {returncode}", resumable=True)
            _append_log(job_id, f"Process failed with exit code {returncode}", level="error", category="process", important=True)

    except Exception as e:
        _mark_job_status(job_id, 'failed', error_summary=f"Execution error: {str(e)}", resumable=True)
        _append_log(job_id, f"Execution error: {str(e)}", level="error", category="process", important=True)
    finally:
        # Never leave the worker subprocess running or registered after we return.
        if process is not None:
            _stop_process(process)
            _unregister_job_process(job_id, process)

async def _save_upload_with_limit(file: UploadFile, dest_path: str, cleanup_dirs: Optional[List[str]] = None):
    """Stream an UploadFile to dest_path in 1MB chunks, enforcing MAX_FILE_SIZE_MB.

    On overflow, removes the partial file plus any cleanup_dirs and raises HTTP 413.
    """
    size = 0
    limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

    with open(dest_path, "wb") as buffer:
        while content := await file.read(1024 * 1024):  # Read 1MB chunks
            size += len(content)
            if size > limit_bytes:
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                for d in (cleanup_dirs or []):
                    shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
            buffer.write(content)

# Below this height the quality gate asks the user before starting (0 = off).
QUALITY_GATE_MIN_HEIGHT = int(os.environ.get("QUALITY_GATE_MIN_HEIGHT", "720"))
QUALITY_PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_probe.py")


async def _probe_youtube_quality(url: str) -> dict:
    """Run quality_probe.py in a worker thread; {} on any failure (fail-open)."""
    def _run():
        try:
            proc = subprocess.run(
                [sys.executable, QUALITY_PROBE_SCRIPT, "--url", url],
                capture_output=True, timeout=75,
            )
            return json.loads(proc.stdout.decode(errors="replace").strip() or "{}")
        except Exception as e:
            print(f"⚠️ Quality probe failed ({e}); starting job without gate.")
            return {}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


@app.post("/api/process")
async def process_endpoint(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    output_format: Optional[str] = Form(None),
    layout_style: Optional[str] = Form(None)
):
    api_key = request.headers.get("X-Gemini-Key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Gemini-Key header")

    # Handle JSON body manually for URL payload
    force_low_quality = False
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        url = body.get("url")
        force_low_quality = bool(body.get("force_low_quality"))
        output_format = body.get("output_format")
        layout_style = body.get("layout_style")

    output_format = normalize_output_format(output_format)
    if layout_style not in ("zoom", "wide"):
        layout_style = "smart"

    if not url and not file:
        raise HTTPException(status_code=400, detail="Must provide URL or File")

    # Pre-flight quality gate: probe the offered resolution BEFORE starting the
    # job, so the user can abort (refresh cookies / update yt-dlp) instead of
    # burning 20 minutes of processing on a 360p-only source. Fail-open: any
    # probe error just starts the job normally.
    if url and not force_low_quality and QUALITY_GATE_MIN_HEIGHT > 0:
        probe = await _probe_youtube_quality(url)
        max_height = int(probe.get("max_height") or 0)
        if 0 < max_height < QUALITY_GATE_MIN_HEIGHT:
            print(f"⚠️ Quality gate: only {max_height}p available for {url} — asking user before starting.")
            return JSONResponse({
                "needs_confirmation": True,
                "quality_check": {
                    "max_height": max_height,
                    "min_height": QUALITY_GATE_MIN_HEIGHT,
                    "cookies_invalid": bool(probe.get("cookies_invalid")),
                },
            })

    job_id = str(uuid.uuid4())
    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    # Prepare Command
    cmd = [sys.executable, "-u", "main.py"] # -u for unbuffered, use same Python as server
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key # Override with key from request

    source_type = "url" if url else "file"
    input_path = None
    input_filename = None
    if url:
        cmd.extend(["-u", url])
    else:
        # Save uploaded file with size limit check
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
        input_filename = file.filename

        await _save_upload_with_limit(file, input_path, cleanup_dirs=[job_output_dir])

        cmd.extend(["-i", input_path])

    cmd.extend(["-o", job_output_dir])
    cmd.extend(["--format", output_format])
    cmd.extend(["--layout", layout_style])

    # Enqueue Job
    jobs[job_id] = _build_job_state(
        job_id,
        output_dir=job_output_dir,
        source_type=source_type,
        source_url=url,
        input_path=input_path,
        input_filename=input_filename,
        status="queued",
    )
    jobs[job_id].update({
        'cmd': cmd,
        'env': env,
        'execution_id': uuid.uuid4().hex,
    })
    _append_log(job_id, f"Job {job_id} queued.", category="queue", important=True)

    await job_queue.put(job_id)

    return {"job_id": job_id, "status": "queued"}

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = _get_job(job_id)
    if job:
        if not job.get("result"):
            _refresh_job_result(job_id, job.get("output_dir", os.path.join(OUTPUT_DIR, job_id)))
        return _build_status_payload(job)

    tombstone = _get_job_tombstone(job_id)
    if tombstone:
        return JSONResponse(status_code=410, content=tombstone)

    raise HTTPException(status_code=404, detail="Job not found")


def _build_support_log_text(job: dict) -> str:
    lines = [
        f"Job ID: {job.get('job_id')}",
        f"Status: {job.get('status')}",
        f"Phase: {job.get('phase_label') or job.get('phase')}",
        f"Progress: {job.get('progress_percent')}",
        f"Phase ETA Seconds: {job.get('phase_eta_seconds')}",
        f"ETA State: {job.get('eta_state')}",
        f"Elapsed Seconds: {job.get('elapsed_seconds')}",
        f"Actual Duration Seconds: {job.get('actual_duration_seconds')}",
        f"Finished At: {job.get('finished_at')}",
        f"Phase Durations Seconds: {job.get('phase_durations_seconds') or {}}",
        f"Seconds Since Completion: {job.get('seconds_since_finish')}",
        f"Last Heartbeat Age: {job.get('seconds_since_heartbeat')}",
        f"Stall State: {job.get('stall_state')}",
        f"Attempt: {job.get('attempt')}",
        f"Resume Count: {job.get('resume_count')}",
        f"Video Duration Seconds: {job.get('video_duration_seconds')}",
        f"Source Type: {job.get('source_type')}",
        f"Source URL: {job.get('source_url') or ''}",
        f"Processing Mode: {job.get('processing_mode') or ''}",
        f"Analysis Status: {job.get('analysis_status') or ''}",
        f"Analysis Coverage: {job.get('analysis_coverage') or {}}",
        f"Error Summary: {job.get('error_summary') or ''}",
        "Warnings:",
    ]
    warnings = job.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings[-20:])
    else:
        lines.append("- none")

    lines.append("Artifacts:")
    artifacts = job.get("artifacts") or {}
    if artifacts:
        lines.extend(f"- {kind}: {path}" for kind, path in sorted(artifacts.items()))
    else:
        lines.append("- none")

    lines.append("Important Logs:")
    important_logs = job.get("important_logs") or []
    if important_logs:
        for entry in important_logs[-200:]:
            lines.append(f"[{entry.get('iso_timestamp')}] [{entry.get('level')}] {entry.get('message')}")
    else:
        lines.append("- none")
    return "\n".join(lines)


@app.get("/api/jobs/{job_id}/support-log")
async def get_support_log(job_id: str):
    job = _get_job(job_id)
    if not job:
        tombstone = _get_job_tombstone(job_id)
        if tombstone:
            return JSONResponse(
                status_code=410,
                content={
                    "job_id": job_id,
                    "status": "archived",
                    "support_log": f"Job {job_id} was already archived.",
                },
            )
        raise HTTPException(status_code=404, detail="Job not found")

    payload = _build_status_payload(job)
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "support_log": _build_support_log_text(payload),
    }


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a queued/running job: flag it, kill its worker subprocess, free the slot."""
    async with _get_job_resume_lock(job_id):
        job = jobs.get(job_id)
        if not job:
            # Not live in memory: exists on disk => already finished; otherwise unknown.
            if _get_job(job_id):
                return {"success": False, "detail": "already finished"}
            raise HTTPException(status_code=404, detail="Job not found")

        if job.get("status") in TERMINAL_JOB_STATUSES:
            return {"success": False, "detail": "already finished"}

        job["cancel_requested"] = True
        job["auto_resume_pending"] = False
        job.pop("auto_resume_token", None)
        # Killing the registered subprocess unblocks run_job's wait loop; its
        # wrapper finally releases the concurrency semaphore slot.
        await asyncio.get_running_loop().run_in_executor(None, _terminate_job_processes, job_id)
        _mark_job_status(job_id, "failed", error_summary="Cancelled by user", resumable=False)
        _append_log(job_id, "Job cancelled by user.", level="warning", category="cancel", important=True)
        return {"success": True}


async def _resume_job_internal(job_id: str, *, phase: Optional[str] = None,
                               api_key: Optional[str] = None,
                               auto_token: Optional[str] = None,
                               manual: bool = False,
                               reason: str = "Job re-queued for resume."):
    """Re-queue a stalled/failed job. Shared by the endpoint and the watchdog."""
    async with _get_job_resume_lock(job_id):
        job = _get_job(job_id)
        if not job:
            if auto_token:
                return {"job_id": job_id, "status": "skipped"}
            raise HTTPException(status_code=404, detail="Job not found")

        if auto_token:
            # A stale task must not resume a later stall generation.
            if (
                job.get("auto_resume_token") != auto_token
                or not job.get("auto_resume_pending")
                or job.get("status") != "stalled"
            ):
                return {"job_id": job_id, "status": "skipped"}
        else:
            if job.get("status") in ACTIVE_JOB_STATUSES:
                raise HTTPException(status_code=409, detail="Job is already active")
            if not job.get("is_resumable") and job.get("status") != "stalled":
                raise HTTPException(status_code=409, detail="Job is not resumable")

        output_dir = job.get("output_dir") or os.path.join(OUTPUT_DIR, job_id)
        if not os.path.isdir(output_dir):
            job["auto_resume_pending"] = False
            job.pop("auto_resume_token", None)
            _persist_job_state(job_id)
            if auto_token:
                _append_log(
                    job_id,
                    "Automatic restart skipped: job artifacts are gone.",
                    level="error", category="stall", important=True,
                )
                return {"job_id": job_id, "status": "skipped"}
            raise HTTPException(status_code=410, detail="Job artifacts are no longer available")

        # Stop the old tree and prove run_job has unregistered it before a new
        # worker is allowed to touch the same directory.
        await asyncio.get_running_loop().run_in_executor(None, _terminate_job_processes, job_id)
        if not await _wait_for_job_processes_stopped(job_id):
            job["status"] = "stalled"
            job["stall_state"] = "stalled"
            job["auto_resume_pending"] = False
            job.pop("auto_resume_token", None)
            _append_log(
                job_id,
                "Restart aborted because the previous worker could not be stopped safely.",
                level="error", category="stall", important=True,
            )
            if auto_token:
                return {"job_id": job_id, "status": "blocked"}
            raise HTTPException(status_code=503, detail="Previous worker is still running")

        cmd = _build_resume_command(job_id, output_dir, phase)
        env = dict(job.get("env") or os.environ)
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        if manual:
            # Explicit user action grants a fresh automatic-restart budget.
            job["auto_resume_count"] = 0
        job["cmd"] = cmd
        job["env"] = env
        job["execution_id"] = uuid.uuid4().hex
        job.pop("cancel_requested", None)
        job.pop("stall_termination_requested", None)
        job.pop("auto_resume_token", None)
        job["auto_resume_pending"] = False
        job["status"] = "queued"
        job["phase"] = "queued"
        job["phase_label"] = "Queued"
        job["progress_percent"] = min(float(job.get("progress_percent") or 0.0), 99.0)
        job["phase_progress_percent"] = 0.0
        job["attempt"] = 0
        job["stall_state"] = "healthy"
        job["error_summary"] = None
        job["warnings"] = []
        job["resume_count"] = int(job.get("resume_count") or 0) + 1
        job["updated_at"] = _now_ts()
        job["last_heartbeat_at"] = job["updated_at"]
        job["last_work_activity_at"] = job["updated_at"]
        job["operation_name"] = None
        job["operation_deadline_at"] = None
        # Restart the elapsed clock: counting from the original start (incl. a
        # possible multi-hour freeze) makes runtime and ETA meaningless.
        job["started_at"] = job["updated_at"]
        job["finished_at"] = None
        job["actual_duration_seconds"] = None
        job["phase_eta_seconds"] = None
        job["eta_seconds"] = None
        job["total_eta_seconds"] = None
        job["eta_reference_at"] = None
        job["eta_state"] = "calculating"
        job["phase_durations_seconds"] = {}
        job.pop("worker_duration_seconds", None)
        job["is_resumable"] = True
        _append_log(job_id, reason, category="resume", important=True)
        await job_queue.put(job_id)
        return {"job_id": job_id, "status": "queued", "resume_count": job["resume_count"]}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: Request, body: Optional[ResumeRequest] = None):
    api_key = request.headers.get("X-Gemini-Key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Gemini-Key header")

    return await _resume_job_internal(
        job_id,
        phase=body.phase if body else None,
        api_key=api_key,
        manual=True,
        reason="Job re-queued for resume.",
    )

from editor import VideoEditor
from subtitles import generate_srt, generate_ass, burn_layers, generate_srt_from_video
from hooks import prepare_hook_overlay
from translate import translate_video, get_supported_languages
from thumbnail import analyze_video_for_titles, refine_titles, generate_thumbnail, generate_youtube_description

class EditRequest(BaseModel):
    job_id: str
    clip_index: int = Field(ge=0)
    api_key: Optional[str] = None
    input_filename: Optional[str] = None

@app.post("/api/edit")
async def edit_clip(
    req: EditRequest,
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key")
):
    async with _get_clip_operation_lock(req.job_id, req.clip_index):
        return await _edit_clip_locked(req, x_gemini_key)


async def _edit_clip_locked(req: EditRequest, x_gemini_key: Optional[str]):
    # Determine API Key
    final_api_key = req.api_key or x_gemini_key or os.environ.get("GEMINI_API_KEY")
    
    if not final_api_key:
        raise HTTPException(status_code=400, detail="Missing Gemini API Key (Header or Body)")

    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs[req.job_id]
    if not isinstance(job.get('result'), dict) or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")
    if req.clip_index >= len(job['result']['clips']):
        raise HTTPException(status_code=404, detail="Clip not found")

    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r', encoding='utf-8') as f:
        metadata = json.load(f)
    metadata_clips = metadata.get('shorts', [])
    if req.clip_index >= len(metadata_clips):
        raise HTTPException(status_code=404, detail="Clip metadata not found")
    clip_data = metadata_clips[req.clip_index]
        
    try:
        requested_filename = os.path.basename(req.input_filename) if req.input_filename else None
        layer_entry = await _resolve_clip_layer_entry(
            req.job_id, output_dir, req.clip_index, clip_data, requested_filename,
        )
        input_path = _clean_source_path(output_dir, layer_entry)
        filename = os.path.basename(input_path)

        # Auto Edit works on pixels without presentation layers. Stored
        # subtitles/hooks are composed once onto the edited clean source later.
        operation_id = uuid.uuid4().hex[:12]
        edited_filename = f"edited_{operation_id}_{filename}"
        edited_clean_path = os.path.join(output_dir, edited_filename)
        
        # Run editing in a thread to avoid blocking main loop
        # Since VideoEditor uses blocking calls (subprocess, API wait)
        def run_edit():
            editor = VideoEditor(api_key=final_api_key)
            
            # SAFE FILE RENAMING STRATEGY (Avoid UnicodeEncodeError in Docker)
            # Create a safe ASCII filename in the same directory
            safe_filename = f"temp_input_{req.job_id}_{operation_id}.mp4"
            safe_input_path = os.path.join(output_dir, safe_filename)
            
            # Copy original file to safe path
            # (Copy is safer than rename if something crashes, we keep original)
            shutil.copy(input_path, safe_input_path)
            
            try:
                # 1. Upload (using safe path)
                vid_file = editor.upload_video(safe_input_path)
                
                # 2. Get duration
                import cv2
                cap = cv2.VideoCapture(safe_input_path)
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                duration = frame_count / fps if fps else 0
                cap.release()
                
                # Load transcript from metadata
                transcript = None
                try:
                    transcript = _load_transcript_for_job(output_dir, metadata=metadata)
                except Exception as e:
                    print(f"⚠️ Could not load transcript for editing context: {e}")

                # The input is clean, so visual zoom effects are safe; layers
                # will be rendered in final screen coordinates afterwards.
                filter_data = editor.get_ffmpeg_filter(
                    vid_file, duration, fps=fps, width=width, height=height,
                    transcript=transcript, has_captions=False,
                )
                
                # 4. Apply
                # Use safe output name first
                safe_output_path = os.path.join(output_dir, f"temp_output_{req.job_id}_{operation_id}.mp4")
                editor.apply_edits(safe_input_path, safe_output_path, filter_data)
                
                # Move result to final destination (rename works even if dest name has unicode if filesystem supports it, 
                # but python might still struggle if locale is broken? No, os.rename usually handles it better than subprocess args)
                # Actually, output_path is defined above: f"edited_{filename}"
                # If filename has unicode, output_path has unicode.
                # Let's hope shutil.move / os.rename works.
                if os.path.exists(safe_output_path):
                    shutil.move(safe_output_path, edited_clean_path)
                
                return filter_data
            finally:
                # Cleanup temp safe input
                if os.path.exists(safe_input_path):
                    os.remove(safe_input_path)

        # Run in thread pool
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, run_edit)

        candidate_entry = _entry_with_clean_source(layer_entry, edited_filename)
        if candidate_entry.get("subtitle") or candidate_entry.get("hook"):
            output_filename = _layered_filename(candidate_entry, uuid.uuid4().hex[:12])
            rendered_path = os.path.join(output_dir, output_filename)
            await loop.run_in_executor(
                None, _render_stored_layers, output_dir, candidate_entry, rendered_path,
            )
        else:
            output_filename = edited_filename

        candidate_entry["current_render"] = output_filename
        new_video_url = f"/videos/{req.job_id}/{output_filename}"
        await _commit_clip_layer_state(
            req.job_id,
            output_dir,
            req.clip_index,
            candidate_entry,
            new_video_url,
            metadata_path=json_files[0],
        )

        return {
            "success": True,
            "new_video_url": new_video_url,
            "edit_plan": plan
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Edit Error: {e}")
        raise HTTPException(status_code=500, detail="Auto Edit failed. Check the server logs for details.")

# --- Clip layer state: clean source + single-pass presentation layers ------
# Version 2 is indexed by logical clip, not by an ever-changing filename.
# Auto Edit and Translation always consume ``clean_source``; subtitle + hook
# are then composed exactly once. This prevents a rendered subtitle from being
# baked into the next edit and receiving a second subtitle on top.

CLIP_LAYERS_FILE = "clip_layers.json"
CLIP_LAYERS_VERSION = 2
HOOK_SIZE_SCALE = {"S": 0.8, "M": 1.0, "L": 1.3}


def _load_clip_layers(output_dir: str) -> dict:
    path = os.path.join(output_dir, CLIP_LAYERS_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_clip_layers(output_dir: str, layers: dict):
    try:
        _safe_write_json(os.path.join(output_dir, CLIP_LAYERS_FILE), layers)
    except Exception as e:
        print(f"⚠️ Failed to persist clip layers: {e}")
        raise


def _strip_layer_prefixes(filename: str, output_dir: str) -> str:
    """Walk subtitled_<ts>_ / hook_[<ts>_] prefixes back to the base file so
    layers are always re-rendered from the clean clip."""
    while True:
        m = re.match(r'^(?:subtitled_[A-Za-z0-9-]+_|hook_(?:[A-Za-z0-9-]+_)?)(.+)$', filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            break
        filename = m.group(1)
    return filename


def _new_clip_layer_store(legacy_entries: Optional[dict] = None) -> dict:
    return {
        "version": CLIP_LAYERS_VERSION,
        "clips": {},
        # Filename-keyed v1 entries cannot all be assigned to clip indices
        # without metadata for every clip. Keep the unresolved entries until
        # each logical clip is touched and migrated lazily.
        "legacy_entries": dict(legacy_entries or {}),
    }


def _coerce_layer_store(raw: dict) -> dict:
    """Bring whatever is on disk into v2 shape without losing v1 entries."""
    if isinstance(raw, dict) and raw.get("version") == CLIP_LAYERS_VERSION and isinstance(raw.get("clips"), dict):
        if not isinstance(raw.get("legacy_entries"), dict):
            raw["legacy_entries"] = {}
        return raw
    legacy_entries = {
        key: value
        for key, value in (raw.items() if isinstance(raw, dict) else [])
        if isinstance(value, dict)
    }
    return _new_clip_layer_store(legacy_entries)


def _filename_from_clip(clip_data: dict, requested_filename: Optional[str] = None) -> str:
    if requested_filename:
        return os.path.basename(requested_filename)
    current = os.path.basename(str(clip_data.get("video_url") or "").split("/")[-1])
    return current or os.path.basename(str(clip_data.get("output_filename") or ""))


def _resolve_clip_layer_state(
    output_dir: str,
    clip_index: int,
    clip_data: dict,
    requested_filename: Optional[str] = None,
):
    """Return ``(store, entry)`` and lazily migrate filename-keyed v1 data.

    Migration first unwraps presentation-layer prefixes when the underlying
    clean edit/translation still exists, then falls back to metadata's
    original ``output_filename``. Unrelated v1 entries remain available for
    later clips instead of being discarded by the first migrated operation.
    """
    raw = _load_clip_layers(output_dir)
    current_filename = _filename_from_clip(clip_data, requested_filename)
    original_filename = os.path.basename(str(clip_data.get("output_filename") or ""))
    stripped_current = _strip_layer_prefixes(current_filename, output_dir)
    stripped_path = os.path.join(output_dir, stripped_current)
    current_path = os.path.join(output_dir, current_filename)
    recovered_layered_derivative = (
        stripped_current != current_filename
        and os.path.exists(stripped_path)
    )
    current_is_clean_derivative = (
        current_filename.startswith(("edited_", "translated_"))
        and "subtitled_" not in current_filename
        and not re.search(r"(?:^|_)hook_(?:[A-Za-z0-9-]+_)?", current_filename)
        and os.path.exists(current_path)
    )
    fallback_clean = stripped_current if recovered_layered_derivative else ""
    if not fallback_clean and current_is_clean_derivative:
        fallback_clean = current_filename
    if not fallback_clean and os.path.exists(os.path.join(output_dir, original_filename)):
        fallback_clean = original_filename
    if not fallback_clean:
        fallback_clean = stripped_current

    store = _coerce_layer_store(raw)
    legacy_entries = store["legacy_entries"]

    clip_key = str(clip_index)
    if clip_key not in store["clips"]:
        legacy_entry = None
        matched_legacy_key = None
        for key in (stripped_current, fallback_clean, original_filename, current_filename):
            candidate = legacy_entries.get(key) if key else None
            if isinstance(candidate, dict):
                legacy_entry = candidate
                matched_legacy_key = key
                break
        if legacy_entry:
            store["clips"][clip_key] = {
                "clean_source": fallback_clean,
                "current_render": current_filename,
                "subtitle": legacy_entry.get("subtitle"),
                "hook": legacy_entry.get("hook"),
            }
            legacy_entries.pop(matched_legacy_key, None)

    entry = store["clips"].setdefault(clip_key, {
        "clean_source": fallback_clean,
        "current_render": current_filename,
        "subtitle": None,
        "hook": None,
    })
    if not entry.get("clean_source"):
        entry["clean_source"] = fallback_clean or current_filename
    if not entry.get("current_render"):
        entry["current_render"] = current_filename or entry["clean_source"]
    entry.setdefault("subtitle", None)
    entry.setdefault("hook", None)
    return store, entry


async def _resolve_clip_layer_entry(
    job_id: str,
    output_dir: str,
    clip_index: int,
    clip_data: dict,
    requested_filename: Optional[str] = None,
) -> dict:
    """Read + lazily migrate one clip's layer entry under the job lock.

    The migration is persisted immediately so a concurrent operation on
    another clip of the same job re-reads the already-migrated store instead
    of racing its own migration against ours. Only the (deep-copied) entry is
    returned — the store snapshot must not be written back after the encode.
    """
    async with _get_job_state_lock(job_id):
        store, entry = _resolve_clip_layer_state(
            output_dir, clip_index, clip_data, requested_filename,
        )
        _save_clip_layers(output_dir, store)
        return copy.deepcopy(entry)


async def _commit_clip_layer_state(
    job_id: str,
    output_dir: str,
    clip_index: int,
    entry: dict,
    video_url: str,
    *,
    metadata_path: Optional[str] = None,
) -> None:
    """Persist one finished clip operation without clobbering parallel clips.

    Encoding ran unlocked and possibly for minutes, so the store read at the
    start is stale by now. Under the job lock: re-read clip_layers.json,
    merge only this clip's entry (all other clip and legacy entries stay
    untouched), save atomically, then update metadata.json/video_url in the
    same critical section since it is job-wide too.
    """
    async with _get_job_state_lock(job_id):
        store = _coerce_layer_store(_load_clip_layers(output_dir))
        previous_entry = store["clips"].get(str(clip_index))
        store["clips"][str(clip_index)] = entry
        _save_clip_layers(output_dir, store)
        _update_clip_version(
            job_id, clip_index, video_url,
            metadata_path=metadata_path, layers=_clip_layer_summary(entry),
        )
        _prune_replaced_clip_files(output_dir, previous_entry, store)


# Presentation renders are named "<prefix>_<generation id>_<clean source>", so
# every restyle of a clip left a full-length MP4 behind. Trying five preset
# looks on a ten-clip job kept 50 orphaned videos until the whole job was
# purged 24 hours later.
_LAYERED_RENDER_RE = re.compile(r'^(?:subtitled|hook)_[A-Za-z0-9-]+_')
_SUBTITLE_FILE_RE = re.compile(r'^subs_\d+_[A-Za-z0-9-]+\.(?:ass|srt)$')


def _referenced_files(store: dict) -> set:
    """Every file the layer store still points at, across all clips."""
    referenced = set()

    def remember(value):
        name = os.path.basename(str(value or ""))
        if name:
            referenced.add(name)

    for entry in list(store.get("clips", {}).values()) + list(store.get("legacy_entries", {}).values()):
        if not isinstance(entry, dict):
            continue
        remember(entry.get("current_render"))
        remember(entry.get("clean_source"))
        remember((entry.get("subtitle") or {}).get("path"))
    return referenced


def _prune_replaced_clip_files(output_dir: str, previous_entry, store: dict) -> None:
    """Delete only the files THIS clip just replaced.

    Deliberately not a directory sweep: another clip of the same job may be
    minutes into an FFmpeg encode whose output file is not in the store yet,
    and a sweep would delete it out from under the running job. We only ever
    consider the previous entry's own render and subtitle, only when they
    match the generated-name patterns, and only when no other clip still
    references them. Failures are ignored — a leftover file is harmless,
    deleting a live one is not.
    """
    if not isinstance(previous_entry, dict):
        return

    still_referenced = _referenced_files(store)
    candidates = [
        os.path.basename(str(previous_entry.get("current_render") or "")),
        os.path.basename(str((previous_entry.get("subtitle") or {}).get("path") or "")),
    ]

    for filename in candidates:
        if not filename or filename in still_referenced:
            continue
        if not (_LAYERED_RENDER_RE.match(filename) or _SUBTITLE_FILE_RE.match(filename)):
            continue
        try:
            os.remove(os.path.join(output_dir, filename))
        except OSError:
            pass


def _clip_layer_summary(entry: dict) -> dict:
    """What the client needs to offer 'remove this layer' buttons."""
    return {
        "subtitle": bool(entry.get("subtitle")),
        "hook": bool(entry.get("hook")),
    }


def _clean_source_path(output_dir: str, entry: dict) -> str:
    clean_source = os.path.basename(str(entry.get("clean_source") or ""))
    path = os.path.join(output_dir, clean_source)
    if not clean_source or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Clean video source not found: {path}")
    return path


def _stored_subtitle_layer(output_dir: str, entry: dict):
    subtitle = entry.get("subtitle") or {}
    filename = os.path.basename(str(subtitle.get("path") or ""))
    path = os.path.join(output_dir, filename)
    if filename and os.path.exists(path):
        return path, subtitle.get("burn_opts") or {}
    return None, None


def _render_stored_layers(output_dir: str, entry: dict, output_path: str):
    """Render the entry's current subtitle/hook once from its clean source."""
    input_path = _clean_source_path(output_dir, entry)
    subtitle_path, subtitle_burn_opts = _stored_subtitle_layer(output_dir, entry)
    hook_png, hook_x, hook_y = _prepare_hook_layer(input_path, entry.get("hook"))
    if not subtitle_path and not hook_png:
        raise ValueError("No renderable subtitle or hook layer")
    try:
        burn_layers(
            input_path,
            output_path,
            subtitle_path=subtitle_path,
            burn_opts=subtitle_burn_opts,
            hook_png=hook_png,
            hook_x=hook_x,
            hook_y=hook_y,
            hook_entrance=True,
        )
    finally:
        if hook_png and os.path.exists(hook_png):
            os.remove(hook_png)


def _layered_filename(entry: dict, generation_id: str) -> str:
    clean_source = os.path.basename(str(entry.get("clean_source") or "clip.mp4"))
    prefix = "subtitled" if entry.get("subtitle") else "hook"
    return f"{prefix}_{generation_id}_{clean_source}"


def _entry_with_clean_source(
    entry: dict,
    clean_source: str,
    *,
    clear_subtitle: bool = False,
    transcript_source: Optional[str] = None,
) -> dict:
    """Pure state transition shared by edit and translation workflows."""
    updated = dict(entry)
    updated["clean_source"] = os.path.basename(clean_source)
    if clear_subtitle:
        updated["subtitle"] = None
    if transcript_source is not None:
        updated["transcript_source"] = transcript_source
    return updated


def _prepare_hook_layer(input_path: str, hook_layer: Optional[dict]):
    """Best-effort PNG + position for a stored hook layer. Returns
    (png_path_or_None, x, y); failures degrade to no hook instead of
    failing the whole render."""
    if not hook_layer or not hook_layer.get("text"):
        return None, 0, 0
    try:
        scale = HOOK_SIZE_SCALE.get(hook_layer.get("size", "M"), 1.0)
        return prepare_hook_overlay(
            input_path, hook_layer["text"],
            position=hook_layer.get("position", "top"), font_scale=scale)
    except Exception as e:
        print(f"⚠️ Hook layer skipped (render failed): {e}")
        return None, 0, 0


class SubtitleRequest(BaseModel):
    job_id: str
    clip_index: int = Field(ge=0)
    position: Literal["top", "middle", "bottom"] = "bottom"
    font_size: int = Field(default=16, ge=10, le=200)
    font_name: str = Field(default="Verdana", min_length=1, max_length=100)
    font_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    border_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    border_width: int = Field(default=2, ge=0, le=10)
    bg_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    bg_opacity: float = Field(default=0.0, ge=0.0, le=1.0)
    style: Literal["classic", "karaoke"] = "classic"
    preset: Literal["custom", "neon_sweep", "rainbow_word"] = "custom"
    highlight_color: str = Field(default="#FFD700", pattern=r"^#[0-9A-Fa-f]{6}$")
    effect: Literal["none", "glow", "pop", "box", "bounce"] = "none"
    base_opacity: float = Field(default=1.0, ge=0.05, le=1.0)
    uppercase: bool = False
    input_filename: Optional[str] = None

@app.post("/api/subtitle")
async def add_subtitles(req: SubtitleRequest):
    async with _get_clip_operation_lock(req.job_id, req.clip_index):
        return await _add_subtitles_locked(req)


async def _add_subtitles_locked(req: SubtitleRequest):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Reload job data from disk just in case metadata was updated
    job = jobs[req.job_id]
    
    # We need to access metadata.json to get the transcript
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    transcript = _load_transcript_for_job(output_dir, metadata=data)
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata. Please process a new video.")
    data['transcript'] = transcript
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]
    
    requested_filename = os.path.basename(req.input_filename) if req.input_filename else None
    if not requested_filename and not _filename_from_clip(clip_data):
        base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
        requested_filename = f"{base_name}_clip_{req.clip_index + 1}.mp4"
    layer_entry = await _resolve_clip_layer_entry(
        req.job_id, output_dir, req.clip_index, clip_data, requested_filename,
    )
    input_path = _clean_source_path(output_dir, layer_entry)
    clean_filename = os.path.basename(input_path)
        
    # Define outputs
    generation_id = uuid.uuid4().hex[:12]
    # Signature presets are ASS renderers by definition. Keeping this defensive
    # guard makes direct API clients safe even if they leave style at "classic".
    is_karaoke = req.style == "karaoke" or req.preset != "custom"
    srt_filename = f"subs_{req.clip_index}_{generation_id}.{'ass' if is_karaoke else 'srt'}"
    srt_path = os.path.join(output_dir, srt_filename)

    # Style options shared by the karaoke ASS generator paths.
    karaoke_opts = dict(
        alignment=req.position, fontsize=req.font_size, font_name=req.font_name,
        font_color=req.font_color, border_color=req.border_color,
        border_width=req.border_width, highlight_color=req.highlight_color,
        bg_color=req.bg_color, bg_opacity=req.bg_opacity,
        effect=req.effect, base_opacity=req.base_opacity, uppercase=req.uppercase,
        preset=req.preset,
    )

    try:
        # 1. Generate subtitle file (SRT, or karaoke ASS with word highlight)
        # Dubbed media must be transcribed from its current audio. The marker
        # survives later clean-source edits through the v2 layer entry.
        is_dubbed = (
            layer_entry.get("transcript_source") == "media"
            or "translated_" in clean_filename
        )

        if is_dubbed:
            print(f"🎙️ Dubbed video detected, transcribing audio for subtitles...")
            def run_transcribe_srt():
                if is_karaoke:
                    return generate_srt_from_video(input_path, srt_path, style="karaoke", **karaoke_opts)
                return generate_srt_from_video(input_path, srt_path)

            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, run_transcribe_srt)
        elif is_karaoke:
            success = generate_ass(transcript, clip_data['start'], clip_data['end'], srt_path, **karaoke_opts)
        else:
            success = generate_srt(transcript, clip_data['start'], clip_data['end'], srt_path)

        if not success:
             raise HTTPException(status_code=400, detail="No words found for this clip range.")

        # 2. Compose ALL presentation layers from the clean source in one pass.
        # State is only committed after FFmpeg succeeds.
        burn_opts = dict(alignment=req.position, fontsize=req.font_size,
                         font_name=req.font_name, font_color=req.font_color,
                         border_color=req.border_color, border_width=req.border_width,
                         bg_color=req.bg_color, bg_opacity=req.bg_opacity)
        candidate_entry = dict(layer_entry)
        candidate_entry["subtitle"] = {
            "path": srt_filename,
            "burn_opts": burn_opts,
            "style": req.style,
            "effect": req.effect,
            "preset": req.preset,
        }
        output_filename = _layered_filename(candidate_entry, generation_id)
        output_path = os.path.join(output_dir, output_filename)

        def run_burn():
            _render_stored_layers(output_dir, candidate_entry, output_path)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_burn)
        candidate_entry["current_render"] = output_filename

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Subtitle Error: {e}")
        raise HTTPException(status_code=500, detail="Subtitle rendering failed. Check the server logs for details.")

    # 3. Atomically persist the selected derivative for refresh, ZIP download,
    # social posting and restart recovery.
    new_video_url = f"/videos/{req.job_id}/{output_filename}"
    await _commit_clip_layer_state(
        req.job_id,
        output_dir,
        req.clip_index,
        candidate_entry,
        new_video_url,
        metadata_path=json_files[0],
    )

    return {
        "success": True,
        "new_video_url": new_video_url
    }


class RemoveLayerRequest(BaseModel):
    job_id: str
    clip_index: int = Field(ge=0)
    # "all" also drops auto-edit and dubbing by going back to the originally
    # rendered clip; the two named layers keep the current clean source.
    layer: Literal["subtitle", "hook", "all"]


@app.post("/api/clip/remove-layer")
async def remove_clip_layer(req: RemoveLayerRequest):
    """Take a burned-in subtitle or hook back off a clip.

    Applying a layer was a one-way door: the only way out was to overwrite it
    with another one. The clean source and the layer descriptions were already
    tracked per clip, so removal is just re-rendering from that source without
    the dropped layer — or handing back the clean source untouched when no
    layer is left, which needs no encode at all.
    """
    async with _get_clip_operation_lock(req.job_id, req.clip_index):
        return await _remove_clip_layer_locked(req)


async def _remove_clip_layer_locked(req: RemoveLayerRequest):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip_data = clips[req.clip_index]

    layer_entry = await _resolve_clip_layer_entry(
        req.job_id, output_dir, req.clip_index, clip_data,
    )

    candidate_entry = dict(layer_entry)
    if req.layer in ("subtitle", "all"):
        candidate_entry["subtitle"] = None
    if req.layer in ("hook", "all"):
        candidate_entry["hook"] = None
    if req.layer == "all":
        original = os.path.basename(str(clip_data.get("output_filename") or ""))
        if original and os.path.exists(os.path.join(output_dir, original)):
            candidate_entry["clean_source"] = original
            # The original clip carries the source audio again, so a later
            # subtitle run must use the job transcript, not re-transcribe.
            candidate_entry.pop("transcript_source", None)

    if candidate_entry == layer_entry:
        raise HTTPException(status_code=400, detail="This clip has no such layer to remove.")

    try:
        if candidate_entry.get("subtitle") or candidate_entry.get("hook"):
            generation_id = uuid.uuid4().hex[:12]
            output_filename = _layered_filename(candidate_entry, generation_id)
            output_path = os.path.join(output_dir, output_filename)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, _render_stored_layers, output_dir, candidate_entry, output_path,
            )
        else:
            # Nothing left to compose — the clean source *is* the result.
            output_filename = os.path.basename(
                _clean_source_path(output_dir, candidate_entry)
            )
        candidate_entry["current_render"] = output_filename
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Remove layer error: {e}")
        raise HTTPException(status_code=500, detail="Removing the layer failed. Check the server logs for details.")

    new_video_url = f"/videos/{req.job_id}/{output_filename}"
    await _commit_clip_layer_state(
        req.job_id,
        output_dir,
        req.clip_index,
        candidate_entry,
        new_video_url,
        metadata_path=json_files[0],
    )

    return {
        "success": True,
        "new_video_url": new_video_url,
        "layers": _clip_layer_summary(candidate_entry),
    }


@app.get("/api/jobs/{job_id}/download-all")
async def download_all_clips(job_id: str):
    """Bundle the current version of every clip of a job into one ZIP."""
    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Job not found")

    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)

    files = []
    for i, clip in enumerate(data.get('shorts', [])):
        filename = os.path.basename(clip.get('video_url', '').split('/')[-1])
        path = os.path.join(output_dir, filename)
        if filename and os.path.exists(path):
            files.append((i, path))

    if not files:
        raise HTTPException(status_code=404, detail="No clip files found for this job")

    zip_path = os.path.join(output_dir, f"clips_{int(time.time())}.zip")

    def build_zip():
        # Videos are already compressed; store instead of deflate for speed.
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
            for i, path in files:
                zf.write(path, arcname=f"clip_{i + 1:02d}_{os.path.basename(path)}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, build_zip)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"openshorts_clips_{job_id[:8]}.zip",
        background=BackgroundTask(os.remove, zip_path),
    )


class HookRequest(BaseModel):
    job_id: str
    clip_index: int = Field(ge=0)
    text: str
    input_filename: Optional[str] = None
    position: Optional[str] = "top" # top, center, bottom
    size: Optional[str] = "M" # S, M, L

@app.post("/api/hook")
async def add_hook(req: HookRequest):
    async with _get_clip_operation_lock(req.job_id, req.clip_index):
        return await _add_hook_locked(req)


async def _add_hook_locked(req: HookRequest):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs[req.job_id]
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]
    
    requested_filename = os.path.basename(req.input_filename) if req.input_filename else None
    if not requested_filename and not _filename_from_clip(clip_data):
        base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
        requested_filename = f"{base_name}_clip_{req.clip_index + 1}.mp4"
    layer_entry = await _resolve_clip_layer_entry(
        req.job_id, output_dir, req.clip_index, clip_data, requested_filename,
    )
    _clean_source_path(output_dir, layer_entry)

    generation_id = uuid.uuid4().hex[:12]
    candidate_entry = dict(layer_entry)
    candidate_entry["hook"] = {"text": req.text, "position": req.position, "size": req.size}
    output_filename = _layered_filename(candidate_entry, generation_id)
    output_path = os.path.join(output_dir, output_filename)

    try:
        def run_hook():
            _render_stored_layers(output_dir, candidate_entry, output_path)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_hook)
        candidate_entry["current_render"] = output_filename

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Hook Error: {e}")
        raise HTTPException(status_code=500, detail="Hook rendering failed. Check the server logs for details.")

    new_video_url = f"/videos/{req.job_id}/{output_filename}"
    await _commit_clip_layer_state(
        req.job_id,
        output_dir,
        req.clip_index,
        candidate_entry,
        new_video_url,
        metadata_path=json_files[0],
    )

    return {
        "success": True,
        "new_video_url": new_video_url
    }

class TranslateRequest(BaseModel):
    job_id: str
    clip_index: int = Field(ge=0)
    target_language: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z-]+$")
    source_language: Optional[str] = Field(default=None, min_length=2, max_length=20, pattern=r"^[A-Za-z-]+$")
    input_filename: Optional[str] = None

@app.get("/api/translate/languages")
async def get_languages():
    """Return supported languages for translation."""
    return {"languages": get_supported_languages()}

@app.post("/api/translate")
async def translate_clip(
    req: TranslateRequest,
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key")
):
    async with _get_clip_operation_lock(req.job_id, req.clip_index):
        return await _translate_clip_locked(req, x_elevenlabs_key)


async def _translate_clip_locked(req: TranslateRequest, x_elevenlabs_key: Optional[str]):
    """Translate a video clip to a different language using ElevenLabs dubbing."""
    if not x_elevenlabs_key:
        raise HTTPException(status_code=400, detail="Missing X-ElevenLabs-Key header")

    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_data = clips[req.clip_index]

    requested_filename = os.path.basename(req.input_filename) if req.input_filename else None
    if not requested_filename and not _filename_from_clip(clip_data):
        base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
        requested_filename = f"{base_name}_clip_{req.clip_index + 1}.mp4"
    layer_entry = await _resolve_clip_layer_entry(
        req.job_id, output_dir, req.clip_index, clip_data, requested_filename,
    )
    input_path = _clean_source_path(output_dir, layer_entry)
    filename = os.path.basename(input_path)

    # Output video with language suffix
    base, ext = os.path.splitext(filename)
    generation_id = uuid.uuid4().hex[:12]
    translated_filename = f"translated_{req.target_language}_{generation_id}_{base}{ext}"
    translated_path = os.path.join(output_dir, translated_filename)

    try:
        # Run translation in thread pool (blocking API calls)
        def run_translate():
            return translate_video(
                video_path=input_path,
                output_path=translated_path,
                target_language=req.target_language,
                api_key=x_elevenlabs_key,
                source_language=req.source_language,
            )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_translate)

        # A translation changes the spoken language: old subtitles are no
        # longer valid. Keep the hook, and mark future subtitles to transcribe
        # the dubbed audio instead of using original metadata timestamps.
        candidate_entry = _entry_with_clean_source(
            layer_entry,
            translated_filename,
            clear_subtitle=True,
            transcript_source="media",
        )
        if candidate_entry.get("hook"):
            output_filename = _layered_filename(candidate_entry, uuid.uuid4().hex[:12])
            rendered_path = os.path.join(output_dir, output_filename)
            await loop.run_in_executor(
                None, _render_stored_layers, output_dir, candidate_entry, rendered_path,
            )
        else:
            output_filename = translated_filename

        candidate_entry["current_render"] = output_filename

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Translation Error: {e}")
        raise HTTPException(status_code=500, detail="Translation failed. Check the server logs for details.")

    new_video_url = f"/videos/{req.job_id}/{output_filename}"
    await _commit_clip_layer_state(
        req.job_id,
        output_dir,
        req.clip_index,
        candidate_entry,
        new_video_url,
        metadata_path=json_files[0],
    )

    return {
        "success": True,
        "new_video_url": new_video_url
    }

class SocialPostRequest(BaseModel):
    job_id: str
    clip_index: int = Field(ge=0)
    api_key: str
    user_id: str
    platforms: List[str] # ["tiktok", "instagram", "youtube"]
    # Optional overrides if frontend wants to edit them
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_date: Optional[str] = None # ISO-8601 string
    timezone: Optional[str] = "UTC"

import httpx

@app.post("/api/social/post")
async def post_to_socials(req: SocialPostRequest):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs[req.job_id]
    if not isinstance(job.get('result'), dict) or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")
    if req.clip_index >= len(job['result']['clips']):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    try:
        clip = job['result']['clips'][req.clip_index]
        # Video URL is relative /videos/..., we need absolute file path
        # clip['video_url'] is like "/videos/{job_id}/{filename}"
        # We constructed it as: f"/videos/{job_id}/{clip_filename}"
        # And file is at f"{OUTPUT_DIR}/{job_id}/{clip_filename}"
        
        filename = clip['video_url'].split('/')[-1]
        file_path = os.path.join(OUTPUT_DIR, req.job_id, filename)
        
        if not os.path.exists(file_path):
             raise HTTPException(status_code=404, detail=f"Video file not found: {file_path}")

        # Construct parameters for Upload-Post API
        # Fallbacks
        final_title = req.title or clip.get('title', 'Viral Short')
        final_description = req.description or clip.get('video_description_for_instagram') or clip.get('video_description_for_tiktok') or "Check this out!"
        
        # Prepare form data
        url = "https://api.upload-post.com/api/upload"
        headers = {
            "Authorization": f"Apikey {req.api_key}"
        }
        
        # Prepare data as dict (httpx handles lists for multiple values)
        data_payload = {
            "user": req.user_id,
            "title": final_title,
            "platform[]": req.platforms, # Pass list directly
            "async_upload": "true"  # Enable async upload
        }

        # Add scheduling if present
        if req.scheduled_date:
            data_payload["scheduled_date"] = req.scheduled_date
            if req.timezone:
                data_payload["timezone"] = req.timezone
        
        # Add Platform specifics
        if "tiktok" in req.platforms:
             data_payload["tiktok_title"] = final_description
             
        if "instagram" in req.platforms:
             data_payload["instagram_title"] = final_description
             data_payload["media_type"] = "REELS"

        if "youtube" in req.platforms:
             yt_title = req.title or clip.get('video_title_for_youtube_short', final_title)
             data_payload["youtube_title"] = yt_title
             data_payload["youtube_description"] = final_description
             data_payload["privacyStatus"] = "public"

        # Send File. Stream the open file handle (no full read into RAM) and run the
        # blocking multipart upload off the event loop so one post can't freeze the server.
        def _do_upload():
            with open(file_path, "rb") as fh:
                files = {"video": (filename, fh, "video/mp4")}
                with httpx.Client(timeout=120.0) as client:
                    print(f"📡 Sending to Upload-Post for platforms: {req.platforms}")
                    return client.post(url, headers=headers, data=data_payload, files=files)

        response = await asyncio.get_running_loop().run_in_executor(None, _do_upload)

        if response.status_code not in [200, 201, 202]: # Added 201
             print(f"❌ Upload-Post Error: {response.text}")
             raise HTTPException(status_code=response.status_code, detail=f"Vendor API Error: {response.text}")

        return response.json()

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Social Post Exception: {e}")
        raise HTTPException(status_code=500, detail="Social publishing failed. Check the server logs for details.")

@app.get("/api/social/user")
async def get_social_user(api_key: str = Header(..., alias="X-Upload-Post-Key")):
    """Proxy to fetch user ID from Upload-Post"""
    if not api_key:
         raise HTTPException(status_code=400, detail="Missing X-Upload-Post-Key header")
         
    url = "https://api.upload-post.com/api/uploadposts/users"
    print(f"🔍 Fetching User ID from: {url}")
    headers = {"Authorization": f"Apikey {api_key}"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"❌ Upload-Post User Fetch Error: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch user: {resp.text}")
            
            data = resp.json()
            print(f"🔍 Upload-Post User Response: {data}")
            
            user_id = None
            # The structure is {'success': True, 'profiles': [{'username': '...'}, ...]}
            profiles_list = []
            if isinstance(data, dict):
                 raw_profiles = data.get('profiles', [])
                 if isinstance(raw_profiles, list):
                     for p in raw_profiles:
                         username = p.get('username')
                         if username:
                             # Determine connected platforms
                             socials = p.get('social_accounts', {})
                             connected = []
                             # Check typical platforms
                             for platform in ['tiktok', 'instagram', 'youtube']:
                                 account_info = socials.get(platform)
                                 # If it's a dict and typically has data, or just not empty string
                                 if isinstance(account_info, dict):
                                     connected.append(platform)
                             
                             profiles_list.append({
                                 "username": username,
                                 "connected": connected
                             })
            
            if not profiles_list:
                # Fallback if no profiles found
                return {"profiles": [], "error": "No profiles found"}
                
            return {"profiles": profiles_list}
            
            
        except HTTPException:
             raise
        except Exception as e:
             print(f"❌ Upload-Post user lookup failed: {e}")
             raise HTTPException(status_code=500, detail="Could not load social profiles. Check the server logs for details.")

# --- Thumbnail Studio Endpoints ---

@app.post("/api/thumbnail/upload")
async def thumbnail_upload(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
):
    """Upload video and start background Whisper transcription immediately."""
    if not url and not file:
        raise HTTPException(status_code=400, detail="Must provide URL or File")

    session_id = str(uuid.uuid4())
    transcript_event = asyncio.Event()

    # Save file if uploaded directly
    video_path = None
    if file:
        video_path = os.path.join(UPLOAD_DIR, f"thumb_{session_id}_{file.filename}")
        await _save_upload_with_limit(file, video_path)

    # Initialize session
    thumbnail_sessions[session_id] = {
        "created_at": _now_ts(),
        "video_path": video_path,
        "transcript_event": transcript_event,
        "transcript_ready": False,
        "transcript": None,
        "transcript_segments": [],
        "video_duration": 0,
        "language": "en",
        "context": "",
        "titles": [],
        "conversation": [],
        "_url": url,  # Store URL for deferred download
    }
    _persist_thumbnail_session(session_id)

    async def run_background_whisper():
        try:
            vpath = video_path
            # Download YouTube video if URL was provided
            if not vpath and url:
                from main import download_youtube_video
                loop = asyncio.get_event_loop()
                vpath, _ = await loop.run_in_executor(None, download_youtube_video, url, UPLOAD_DIR)
                session = thumbnail_sessions.get(session_id)
                if session is None:
                    return
                session["video_path"] = vpath
                _persist_thumbnail_session(session_id)

            from main import transcribe_video
            loop = asyncio.get_event_loop()
            transcript = await loop.run_in_executor(None, transcribe_video, vpath)
            segments = transcript.get("segments", [])
            duration = segments[-1]["end"] if segments else 0

            session = thumbnail_sessions.get(session_id)
            if session is None:
                return
            session.update({
                "transcript_ready": True,
                "transcript": transcript,
                "transcript_segments": segments,
                "video_duration": duration,
                "language": transcript.get("language", "en"),
            })
            _persist_thumbnail_session(session_id)
            print(f"✅ [Thumbnail] Background Whisper complete for session {session_id}")
        except Exception as e:
            print(f"❌ [Thumbnail] Background Whisper failed: {e}")
            session = thumbnail_sessions.get(session_id)
            if session is not None:
                session["transcript_error"] = str(e)
                _persist_thumbnail_session(session_id)
        finally:
            transcript_event.set()

    asyncio.create_task(run_background_whisper())

    return {"session_id": session_id}


@app.post("/api/thumbnail/analyze")
async def thumbnail_analyze(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key")
):
    """Analyze a video and suggest viral YouTube titles."""
    api_key = x_gemini_key
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Gemini-Key header")

    pre_transcript = None

    # Check for pre-existing session with background Whisper
    if session_id and session_id in thumbnail_sessions:
        session = thumbnail_sessions[session_id]

        # Wait for background Whisper to complete
        transcript_event = session.get("transcript_event")
        if transcript_event:
            print(f"⏳ [Thumbnail] Waiting for background Whisper to finish...")
            await transcript_event.wait()

        if session.get("transcript_error"):
            raise HTTPException(status_code=500, detail=f"Transcription failed: {session['transcript_error']}")

        video_path = session["video_path"]
        if not video_path or not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video file not found in session")

        if session.get("transcript_ready"):
            pre_transcript = session["transcript"]
    else:
        # No pre-existing session — need file or URL
        if not url and not file:
            raise HTTPException(status_code=400, detail="Must provide URL, File, or session_id")

        session_id = str(uuid.uuid4())

        if url:
            from main import download_youtube_video
            loop = asyncio.get_running_loop()
            video_path, _ = await loop.run_in_executor(
                None, download_youtube_video, url, UPLOAD_DIR
            )
        else:
            video_path = os.path.join(UPLOAD_DIR, f"thumb_{session_id}_{file.filename}")
            with open(video_path, "wb") as buffer:
                content = await file.read()
                buffer.write(content)

    try:
        # Run analysis in thread pool (skips Whisper if pre_transcript is available)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, analyze_video_for_titles, api_key, video_path, pre_transcript)

        # Store/update session context
        if session_id not in thumbnail_sessions:
            thumbnail_sessions[session_id] = {"created_at": _now_ts()}

        thumbnail_sessions[session_id].update({
            "context": result.get("transcript_summary", ""),
            "titles": result.get("titles", []),
            "language": result.get("language", "en"),
            "conversation": thumbnail_sessions[session_id].get("conversation", []),
            "video_path": video_path,
            "transcript_segments": result.get("segments", []),
            "video_duration": result.get("video_duration", 0)
        })
        _persist_thumbnail_session(session_id)

        return {
            "session_id": session_id,
            "titles": result.get("titles", []),
            "context": result.get("transcript_summary", ""),
            "language": result.get("language", "en"),
            "recommended": result.get("recommended", [])
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Thumbnail Analyze Error: {e}")
        raise HTTPException(status_code=500, detail="Thumbnail analysis failed. Check the server logs for details.")


class ThumbnailTitlesRequest(BaseModel):
    session_id: Optional[str] = None
    message: Optional[str] = None
    title: Optional[str] = None

@app.post("/api/thumbnail/titles")
async def thumbnail_titles(
    req: ThumbnailTitlesRequest,
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key")
):
    """Refine title suggestions or accept a manual title."""
    api_key = x_gemini_key
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Gemini-Key header")

    # Manual title mode - just create a session with the user's title
    if req.title:
        session_id = req.session_id or str(uuid.uuid4())
        if session_id not in thumbnail_sessions:
            thumbnail_sessions[session_id] = {
                "created_at": _now_ts(),
                "context": "",
                "titles": [req.title],
                "language": "en",
                "conversation": []
            }
        _persist_thumbnail_session(session_id)
        return {"session_id": session_id, "titles": [req.title]}

    # Refinement mode
    if not req.session_id or req.session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    if not req.message:
        raise HTTPException(status_code=400, detail="Must provide message or title")

    session = thumbnail_sessions[req.session_id]

    # Add user message to conversation history
    session["conversation"].append({"role": "user", "content": req.message})

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            refine_titles,
            api_key,
            session["context"],
            req.message,
            session["conversation"]
        )

        new_titles = result.get("titles", [])
        session["titles"] = new_titles
        session["conversation"].append({"role": "assistant", "content": json.dumps(new_titles)})
        _persist_thumbnail_session(req.session_id)

        return {"titles": new_titles}

    except Exception as e:
        print(f"❌ Thumbnail Titles Error: {e}")
        raise HTTPException(status_code=500, detail="Title refinement failed. Check the server logs for details.")


@app.post("/api/thumbnail/generate")
async def thumbnail_generate(
    request: Request,
    session_id: str = Form(...),
    title: str = Form(...),
    extra_prompt: str = Form(""),
    count: int = Form(3),
    face: Optional[UploadFile] = File(None),
    background: Optional[UploadFile] = File(None),
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key")
):
    """Generate YouTube thumbnails with Gemini image generation."""
    api_key = x_gemini_key
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Gemini-Key header")

    # Clamp count
    count = min(max(1, count), 6)

    # Save optional uploaded images
    face_path = None
    bg_path = None
    thumb_upload_dir = os.path.join(UPLOAD_DIR, f"thumb_{session_id}")
    os.makedirs(thumb_upload_dir, exist_ok=True)

    try:
        if face and face.filename:
            face_path = os.path.join(thumb_upload_dir, f"face_{face.filename}")
            with open(face_path, "wb") as f:
                f.write(await face.read())

        if background and background.filename:
            bg_path = os.path.join(thumb_upload_dir, f"bg_{background.filename}")
            with open(bg_path, "wb") as f:
                f.write(await background.read())

        # Get video context from session (transcript summary from analysis step)
        video_context = ""
        if session_id in thumbnail_sessions:
            video_context = thumbnail_sessions[session_id].get("context", "")

        # Run generation in thread pool
        loop = asyncio.get_event_loop()
        thumbnails = await loop.run_in_executor(
            None,
            generate_thumbnail,
            api_key,
            title,
            session_id,
            face_path,
            bg_path,
            extra_prompt,
            count,
            video_context
        )

        if not thumbnails:
            raise HTTPException(status_code=500, detail="Thumbnail generation failed. Please check your Gemini API key has access to image generation (gemini-3.1-flash-image-preview model).")

        return {"thumbnails": thumbnails}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Thumbnail Generate Error: {e}")
        raise HTTPException(status_code=500, detail="Thumbnail generation failed. Check the server logs for details.")


class ThumbnailDescribeRequest(BaseModel):
    session_id: str
    title: str

@app.post("/api/thumbnail/describe")
async def thumbnail_describe(
    req: ThumbnailDescribeRequest,
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key")
):
    """Generate a YouTube description with chapters from the transcript."""
    api_key = x_gemini_key
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Gemini-Key header")

    if req.session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = thumbnail_sessions[req.session_id]
    segments = session.get("transcript_segments", [])
    if not segments:
        raise HTTPException(status_code=400, detail="No transcript segments available. Please analyze a video first.")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            generate_youtube_description,
            api_key,
            req.title,
            segments,
            session.get("language", "en"),
            session.get("video_duration", 0)
        )
        return {"description": result.get("description", "")}

    except Exception as e:
        print(f"❌ Thumbnail Describe Error: {e}")
        raise HTTPException(status_code=500, detail="Description generation failed. Check the server logs for details.")


@app.post("/api/thumbnail/publish")
async def thumbnail_publish(
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    thumbnail_url: str = Form(...),
    api_key: str = Form(...),
    user_id: str = Form(...),
):
    """Kick off a background upload to YouTube via Upload-Post and return immediately."""
    if session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = thumbnail_sessions[session_id]
    video_path = session.get("video_path")
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Original video file not found")

    # Resolve thumbnail path from URL
    thumb_relative = thumbnail_url.lstrip("/")
    if thumb_relative.startswith("thumbnails/"):
        thumb_path = os.path.join(OUTPUT_DIR, thumb_relative)
    else:
        thumb_path = os.path.join(THUMBNAILS_DIR, thumb_relative)

    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail=f"Thumbnail file not found: {thumb_path}")

    # Generate a unique ID for this publish job so the frontend can poll
    publish_id = str(uuid.uuid4())
    publish_jobs[publish_id] = {"status": "uploading", "result": None, "error": None, "created_at": _now_ts()}
    _persist_publish_job(publish_id)

    def do_upload():
        """Runs in a thread via BackgroundTasks — does the actual multipart upload."""
        try:
            upload_url = "https://api.upload-post.com/api/upload"
            headers = {"Authorization": f"Apikey {api_key}"}
            data_payload = {
                "user": user_id,
                "platform[]": ["youtube"],
                "title": title,          # required base field (fallback)
                "async_upload": "true",
                "youtube_title": title,
                "youtube_description": description,
                "privacyStatus": "public",
            }
            video_filename = os.path.basename(video_path)
            thumb_filename = os.path.basename(thumb_path)

            print(f"📡 [Thumbnail] Publishing to YouTube via Upload-Post... (publish_id={publish_id})")
            # Stream the open handles (video can be up to 2GB) instead of reading into RAM.
            # Use a long timeout — video uploads can take several minutes.
            with open(video_path, "rb") as vf, open(thumb_path, "rb") as tf:
                files = {
                    "video": (video_filename, vf, "video/mp4"),
                    "thumbnail": (thumb_filename, tf, "image/jpeg"),
                }
                with httpx.Client(timeout=600.0) as client:
                    response = client.post(upload_url, headers=headers, data=data_payload, files=files)

            if response.status_code not in [200, 201, 202]:
                err = f"Upload-Post API Error ({response.status_code}): {response.text}"
                print(f"❌ {err}")
                publish_jobs[publish_id]["status"] = "failed"
                publish_jobs[publish_id]["error"] = err
                _persist_publish_job(publish_id)
            else:
                print(f"✅ [Thumbnail] Published successfully (publish_id={publish_id})")
                publish_jobs[publish_id]["status"] = "done"
                publish_jobs[publish_id]["result"] = response.json()
                _persist_publish_job(publish_id)

        except Exception as e:
            err = str(e)
            print(f"❌ Thumbnail Publish Background Error: {err}")
            publish_jobs[publish_id]["status"] = "failed"
            publish_jobs[publish_id]["error"] = err
            _persist_publish_job(publish_id)

    background_tasks.add_task(do_upload)
    return {"publish_id": publish_id, "status": "uploading"}


@app.get("/api/thumbnail/publish/status/{publish_id}")
async def thumbnail_publish_status(publish_id: str):
    """Poll the status of a background publish job."""
    if publish_id not in publish_jobs:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return publish_jobs[publish_id]


# @app.get("/api/gallery/clips")
# async def get_gallery_clips(limit: int = 20, offset: int = 0, refresh: bool = False):
#     """
#     Fetch clips from S3 for the gallery with pagination.
#
#     Args:
#         limit: Number of clips to return (default 20, max 100)
#         offset: Starting position for pagination
#         refresh: Force refresh cache
#     """
#     try:
#         # Clamp limit to reasonable values
#         limit = min(max(1, limit), 100)
#
#         # Get clips (uses cache internally)
#         all_clips = list_all_clips(limit=limit + offset, force_refresh=refresh)
#
#         # Apply offset for pagination
#         clips = all_clips[offset:offset + limit]
#
#         return {
#             "clips": clips,
#             "total": len(all_clips),
#             "limit": limit,
#             "offset": offset,
#             "has_more": len(all_clips) > offset + limit
#         }
#     except Exception as e:
#         print(f"❌ Gallery Error: {e}")
#         raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════
# SaaSShorts: AI UGC Video Generator for SaaS Products
# ═══════════════════════════════════════════════════════════════════════

from saasshorts import (
    scrape_website,
    research_saas_online,
    analyze_saas,
    generate_scripts,
    generate_full_video,
    generate_actor_images,
    get_elevenlabs_voices,
    DEFAULT_VOICES,
)

# State for SaaSShorts jobs (separate from video processing jobs)
saas_jobs: Dict[str, Dict] = {}


class SaaSAnalyzeRequest(BaseModel):
    url: Optional[str] = None
    description: Optional[str] = None  # Manual product/business description
    num_scripts: int = 3
    style: str = "ugc"
    language: str = "en"
    actor_gender: str = "female"


@app.post("/api/saasshorts/analyze")
async def saasshorts_analyze(
    req: SaaSAnalyzeRequest,
    x_gemini_key: Optional[str] = Header(None, alias="X-Gemini-Key"),
):
    """Analyze a URL or manual description and generate video scripts."""
    gemini_key = x_gemini_key or os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        raise HTTPException(status_code=400, detail="Missing Gemini API Key")

    if not req.url and not req.description:
        raise HTTPException(status_code=400, detail="Provide a URL or a product description")

    try:
        loop = asyncio.get_event_loop()

        def run_analysis():
            web_research = None

            if req.url and req.url.strip():
                # URL provided: full scrape + research pipeline
                scraped = scrape_website(req.url)
                web_research = research_saas_online(req.url, gemini_key)
                analysis = analyze_saas(scraped, gemini_key, web_research=web_research)
            else:
                # Manual description: build analysis from description
                analysis = {
                    "product_name": req.description.split(",")[0].strip()[:60] if req.description else "Product",
                    "description": req.description,
                    "value_proposition": req.description,
                    "target_audience": "general audience",
                    "key_features": [req.description],
                    "pain_points": [],
                    "tone": "casual and authentic",
                }

            scripts = generate_scripts(analysis, gemini_key, req.num_scripts, req.style, req.language, req.actor_gender)
            return {
                "analysis": analysis,
                "scripts": scripts,
                "web_research": web_research,
            }

        result = await loop.run_in_executor(None, run_analysis)
        return result

    except Exception as e:
        print(f"❌ SaaS analysis failed: {e}")
        raise HTTPException(status_code=500, detail="SaaS analysis failed. Check the server logs for details.")


class SaaSActorRequest(BaseModel):
    actor_description: str
    num_options: int = 3
    product_description: Optional[str] = None


@app.post("/api/saasshorts/actor-upload")
async def saasshorts_actor_upload(file: UploadFile = File(...)):
    """Upload a custom actor image (stored locally only, not S3)."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        content = await file.read()

        # Validate minimum size
        if len(content) < 1000:
            raise HTTPException(status_code=400, detail="File too small to be a valid image")

        upload_id = uuid.uuid4().hex[:8]
        upload_dir = os.path.join(OUTPUT_DIR, "actor_uploads")
        os.makedirs(upload_dir, exist_ok=True)
        filename = f"custom_{upload_id}.png"
        file_path = os.path.join(upload_dir, filename)

        with open(file_path, "wb") as f:
            f.write(content)

        return {"url": f"/videos/actor_uploads/{filename}"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Actor upload failed: {e}")
        raise HTTPException(status_code=500, detail="Actor upload failed. Check the server logs for details.")


@app.post("/api/saasshorts/actor-options")
async def saasshorts_actor_options(
    req: SaaSActorRequest,
    x_fal_key: Optional[str] = Header(None, alias="X-Fal-Key"),
):
    """Generate multiple actor image options for the user to choose from."""
    fal_key = x_fal_key
    if not fal_key:
        raise HTTPException(status_code=400, detail="Missing fal.ai API Key")

    try:
        job_id = str(uuid.uuid4())
        out_dir = os.path.join(OUTPUT_DIR, f"saas_actors_{job_id}")
        os.makedirs(out_dir, exist_ok=True)

        loop = asyncio.get_running_loop()
        import functools
        paths = await loop.run_in_executor(
            None,
            functools.partial(
                generate_actor_images,
                req.actor_description, fal_key, out_dir, "actor", req.num_options,
                product_description=req.product_description,
            ),
        )

        # Upload each actor image to public S3 with description
        desc = req.actor_description
        if req.product_description:
            desc += f" (holding {req.product_description})"
        urls = []
        for p in paths:
            s3_url = upload_actor_to_s3(p, description=desc)
            if s3_url:
                urls.append(s3_url)
            else:
                # Fallback to local URL if S3 fails
                urls.append(f"/videos/saas_actors_{job_id}/{os.path.basename(p)}")

        return {"images": urls}

    except Exception as e:
        print(f"❌ Actor generation failed: {e}")
        raise HTTPException(status_code=500, detail="Actor generation failed. Check the server logs for details.")


@app.get("/api/saasshorts/gallery")
async def saasshorts_video_gallery(limit: int = 50):
    """List all UGC videos from the public gallery."""
    try:
        loop = asyncio.get_running_loop()
        videos = await loop.run_in_executor(None, list_video_gallery, limit)
        return {"videos": videos, "total": len(videos)}
    except Exception as e:
        print(f"❌ SaaS gallery failed: {e}")
        raise HTTPException(status_code=500, detail="Could not load the SaaS gallery. Check the server logs for details.")


class SaaSPostRequest(BaseModel):
    job_id: str
    api_key: str
    user_id: str
    platforms: List[str]
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_date: Optional[str] = None
    timezone: Optional[str] = "UTC"


@app.post("/api/saasshorts/post")
async def saasshorts_post_to_socials(req: SaaSPostRequest):
    """Post an AI Shorts video to social media via Upload-Post."""
    if req.job_id not in saas_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = saas_jobs[req.job_id]
    result = job.get("result")
    if not result or not result.get("video_url"):
        raise HTTPException(status_code=400, detail="No video available for this job")

    try:
        # Resolve video file path
        video_url = result["video_url"]  # e.g. /videos/saas_xxx/slug_final.mp4
        rel_path = video_url.replace("/videos/", "")
        file_path = os.path.join(OUTPUT_DIR, rel_path)

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"Video file not found")

        script = result.get("script", {})
        final_title = req.title or script.get("title", "AI Short")
        final_description = req.description or script.get("caption", "")
        if not final_description:
            final_description = script.get("full_narration", "Check this out!")

        url = "https://api.upload-post.com/api/upload"
        headers = {"Authorization": f"Apikey {req.api_key}"}

        data_payload = {
            "user": req.user_id,
            "title": final_title,
            "platform[]": req.platforms,
            "async_upload": "true",
        }

        if req.scheduled_date:
            data_payload["scheduled_date"] = req.scheduled_date
            if req.timezone:
                data_payload["timezone"] = req.timezone

        if "tiktok" in req.platforms:
            data_payload["tiktok_title"] = final_description
        if "instagram" in req.platforms:
            data_payload["instagram_title"] = final_description
            data_payload["media_type"] = "REELS"
        if "youtube" in req.platforms:
            data_payload["youtube_title"] = final_title
            data_payload["youtube_description"] = final_description
            data_payload["privacyStatus"] = "public"

        filename = os.path.basename(file_path)

        # Stream the open file handle and run the blocking upload off the event loop.
        def _do_upload():
            with open(file_path, "rb") as fh:
                files = {"video": (filename, fh, "video/mp4")}
                with httpx.Client(timeout=120.0) as client:
                    print(f"📡 [AI Shorts] Sending to Upload-Post: {req.platforms}")
                    return client.post(url, headers=headers, data=data_payload, files=files)

        response = await asyncio.get_running_loop().run_in_executor(None, _do_upload)

        if response.status_code not in [200, 201, 202]:
            raise HTTPException(status_code=response.status_code, detail=f"Upload-Post Error: {response.text}")

        return response.json()

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [AI Shorts] Post Exception: {e}")
        raise HTTPException(status_code=500, detail="AI Shorts publishing failed. Check the server logs for details.")


@app.get("/gallery", response_class=HTMLResponse)
async def gallery_html_page():
    """SEO gallery page with all generated UGC videos."""
    import html as html_mod
    loop = asyncio.get_running_loop()
    videos = await loop.run_in_executor(None, list_video_gallery, 100)

    cards_html = ""
    ld_items = []
    for i, v in enumerate(videos):
        title = html_mod.escape(v.get("title", "Untitled"))
        video_url = v.get("video_url", "")
        actor_url = v.get("actor_url", "")
        video_id = v.get("video_id", "")
        duration = v.get("duration", 0)
        mode = v.get("video_mode", "")
        product = html_mod.escape(v.get("product_name", ""))
        caption = html_mod.escape(v.get("caption", "")[:120])

        mode_badge = '<span style="background:#22c55e;color:#000;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:700">LOW COST</span>' if mode == "lowcost" else '<span style="background:#8b5cf6;color:#fff;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:700">PREMIUM</span>'

        cards_html += f'''
        <a href="/video/{video_id}" style="text-decoration:none;color:inherit">
          <div style="background:#18181b;border-radius:16px;overflow:hidden;border:1px solid #27272a;transition:transform 0.2s" onmouseover="this.style.transform='scale(1.02)'" onmouseout="this.style.transform='scale(1)'">
            <div style="position:relative;aspect-ratio:9/16;background:#000">
              <video src="{video_url}" poster="{actor_url}" muted playsinline preload="metadata"
                     onmouseenter="this.play()" onmouseleave="this.pause();this.currentTime=0"
                     style="width:100%;height:100%;object-fit:cover"></video>
              <div style="position:absolute;top:8px;right:8px">{mode_badge}</div>
            </div>
            <div style="padding:12px">
              <h2 style="font-size:14px;font-weight:600;margin:0 0 4px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{title}</h2>
              <p style="font-size:11px;color:#71717a;margin:0">{duration:.0f}s · {product}</p>
            </div>
          </div>
        </a>'''

        ld_items.append(f'{{"@type":"ListItem","position":{i+1},"url":"https://openshorts.app/video/{video_id}","name":"{title}"}}')

    ld_json = f'{{"@context":"https://schema.org","@type":"CollectionPage","name":"AI UGC Video Gallery","mainEntity":{{"@type":"ItemList","numberOfItems":{len(videos)},"itemListElement":[{",".join(ld_items)}]}}}}'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI UGC Video Gallery | OpenShorts</title>
<meta name="description" content="Browse {len(videos)} AI-generated UGC marketing videos. Create viral TikTok and Instagram Reels for your SaaS product.">
<meta name="robots" content="index, follow">
<meta property="og:title" content="AI UGC Video Gallery | OpenShorts">
<meta property="og:type" content="website">
<meta property="og:description" content="Browse AI-generated UGC marketing videos for SaaS products.">
<script type="application/ld+json">{ld_json}</script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0c;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,sans-serif}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:20px;padding:20px;max-width:1400px;margin:0 auto}}
nav{{padding:20px 40px;border-bottom:1px solid #27272a;display:flex;align-items:center;justify-content:space-between}}
h1{{font-size:28px;font-weight:700;padding:40px 20px 0;text-align:center}}
.subtitle{{text-align:center;color:#71717a;font-size:14px;padding:8px 20px 20px}}
.cta{{display:inline-block;background:#8b5cf6;color:#fff;padding:10px 24px;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px}}
</style>
</head>
<body>
<nav><strong style="font-size:18px">OpenShorts</strong><a href="/" class="cta">Create Your Video</a></nav>
<h1>AI-Generated UGC Videos</h1>
<p class="subtitle">{len(videos)} videos generated · Low Cost & Premium modes</p>
<div class="grid">{cards_html}</div>
<div style="text-align:center;padding:40px"><a href="/" class="cta">Create Your Own UGC Video</a></div>
</body></html>'''


@app.get("/video/{video_id}", response_class=HTMLResponse)
async def video_html_page(video_id: str):
    """SEO individual video page with og:video meta tags."""
    import html as html_mod
    loop = asyncio.get_running_loop()
    videos = await loop.run_in_executor(None, list_video_gallery, 200)
    meta = next((v for v in videos if v.get("video_id") == video_id), None)
    if not meta:
        raise HTTPException(status_code=404, detail="Video not found")

    title = html_mod.escape(meta.get("title", "Untitled"))
    caption = html_mod.escape(meta.get("caption", ""))
    narration = html_mod.escape(meta.get("full_narration", ""))
    video_url = meta.get("video_url", "")
    actor_url = meta.get("actor_url", "")
    duration = meta.get("duration", 0)
    mode = meta.get("video_mode", "")
    product = html_mod.escape(meta.get("product_name", ""))
    product_url = html_mod.escape(meta.get("product_url", ""))
    language = meta.get("language", "en")
    hashtags = " ".join(meta.get("hashtags", []))
    cost = meta.get("cost_estimate", {}).get("total", 0)
    created = meta.get("created_at", "")
    actor_desc = html_mod.escape(meta.get("actor_description", ""))

    ld_json = f'{{"@context":"https://schema.org","@type":"VideoObject","name":"{title}","description":"{caption}","thumbnailUrl":"{actor_url}","contentUrl":"{video_url}","uploadDate":"{created}","duration":"PT{int(duration)}S","width":1080,"height":1920,"inLanguage":"{language}"}}'

    mode_label = "Low Cost" if mode == "lowcost" else "Premium"

    return f'''<!DOCTYPE html>
<html lang="{language}">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - AI UGC Video | OpenShorts</title>
<meta name="description" content="{caption} {hashtags}">
<meta property="og:type" content="video.other">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{caption}">
<meta property="og:video" content="{video_url}">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="1080">
<meta property="og:video:height" content="1920">
<meta property="og:image" content="{actor_url}">
<meta name="twitter:card" content="player">
<meta name="twitter:title" content="{title}">
<meta name="twitter:image" content="{actor_url}">
<script type="application/ld+json">{ld_json}</script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0c;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,sans-serif}}
nav{{padding:20px 40px;border-bottom:1px solid #27272a;display:flex;align-items:center;gap:16px}}
nav a{{color:#a1a1aa;text-decoration:none;font-size:14px}}
.container{{max-width:1000px;margin:0 auto;padding:40px 20px;display:grid;grid-template-columns:1fr 1fr;gap:40px}}
@media(max-width:768px){{.container{{grid-template-columns:1fr}}}}
video{{width:100%;border-radius:16px;background:#000}}
h1{{font-size:22px;font-weight:700;margin-bottom:8px}}
.meta{{color:#71717a;font-size:13px;margin-bottom:20px}}
.section{{margin-bottom:20px}}
.section h2{{font-size:13px;color:#71717a;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
.section p{{font-size:14px;line-height:1.6}}
.badge{{display:inline-block;padding:3px 10px;border-radius:9999px;font-size:11px;font-weight:700}}
.cta{{display:inline-block;background:#8b5cf6;color:#fff;padding:10px 24px;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px;margin-top:20px}}
</style>
</head>
<body>
<nav><strong>OpenShorts</strong><a href="/gallery">Gallery</a><span style="color:#3f3f46">›</span><span style="color:#e4e4e7;font-size:14px">{title}</span></nav>
<div class="container">
<div><video src="{video_url}" poster="{actor_url}" controls autoplay playsinline style="aspect-ratio:9/16;object-fit:cover"></video></div>
<div>
<h1>{title}</h1>
<p class="meta">{duration:.0f}s · {mode_label} · ${cost:.2f} · {product}</p>
<div class="section"><h2>Caption</h2><p>{caption}</p><p style="color:#8b5cf6;margin-top:4px">{hashtags}</p></div>
<div class="section"><h2>Script</h2><p>{narration}</p></div>
<div class="section"><h2>Actor</h2><p>{actor_desc}</p></div>
{f'<div class="section"><h2>Product</h2><p><a href="{product_url}" style="color:#8b5cf6" target="_blank">{product}</a></p></div>' if product_url else ''}
<a href="/gallery">← Back to Gallery</a>
<br><a href="/" class="cta">Create Your Own</a>
</div>
</div>
</body></html>'''


@app.get("/api/saasshorts/actor-gallery")
async def saasshorts_actor_gallery():
    """List all previously generated actor images from public S3."""
    try:
        loop = asyncio.get_running_loop()
        images = await loop.run_in_executor(None, list_actor_gallery)
        return {"images": images}
    except Exception as e:
        print(f"❌ Actor gallery failed: {e}")
        raise HTTPException(status_code=500, detail="Could not load the actor gallery. Check the server logs for details.")


class SaaSGenerateRequest(BaseModel):
    script: dict
    voice_id: Optional[str] = None
    actor_description: Optional[str] = None
    selected_actor_url: Optional[str] = None  # Pre-selected actor image URL
    retry_job_id: Optional[str] = None
    video_mode: str = "lowcost"  # "lowcost" or "premium"


@app.post("/api/saasshorts/generate")
async def saasshorts_generate(
    req: SaaSGenerateRequest,
    x_fal_key: Optional[str] = Header(None, alias="X-Fal-Key"),
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key"),
):
    """Generate a SaaS UGC video from a script. Returns a job_id for polling."""
    fal_key = x_fal_key
    elevenlabs_key = x_elevenlabs_key

    if not fal_key:
        raise HTTPException(status_code=400, detail="Missing fal.ai API Key (X-Fal-Key header)")
    if not elevenlabs_key:
        raise HTTPException(status_code=400, detail="Missing ElevenLabs API Key (X-ElevenLabs-Key header)")

    # Support retry: reuse output_dir so cached assets (image, voice, head, broll) are kept
    reused = False
    if req.retry_job_id:
        # Check memory first, then disk
        old_dir = os.path.join(OUTPUT_DIR, f"saas_{req.retry_job_id}")
        if req.retry_job_id in saas_jobs:
            old_dir = saas_jobs[req.retry_job_id]["output_dir"]

        if os.path.isdir(old_dir):
            job_id = req.retry_job_id
            job_output_dir = old_dir
            reused = True
            # Clear the 0-byte final video so pipeline re-generates it
            for f in os.listdir(old_dir):
                fp = os.path.join(old_dir, f)
                if f.endswith("_final.mp4") and os.path.getsize(fp) == 0:
                    os.remove(fp)
            saas_jobs[job_id] = {
                "status": "processing",
                "logs": [f"Retrying job {job_id[:8]}... reusing cached assets from disk."],
                "result": None,
                "output_dir": job_output_dir,
            }
            _persist_saas_job(job_id)

    if not reused:
        job_id = str(uuid.uuid4())
        job_output_dir = os.path.join(OUTPUT_DIR, f"saas_{job_id}")
        os.makedirs(job_output_dir, exist_ok=True)
        saas_jobs[job_id] = {
            "status": "processing",
            "logs": ["SaaSShorts job started."],
            "result": None,
            "output_dir": job_output_dir,
        }
        _persist_saas_job(job_id)

    # If user selected a pre-generated actor, resolve it to a local path
    selected_actor_path = None
    if req.selected_actor_url:
        if req.selected_actor_url.startswith("http"):
            # Download from S3 public URL to job output dir (off the event loop).
            import httpx

            def _download_actor():
                actor_local = os.path.join(job_output_dir, "selected_actor.png")
                with httpx.Client(timeout=30.0) as client:
                    resp = client.get(req.selected_actor_url)
                    if resp.status_code == 200:
                        with open(actor_local, "wb") as f:
                            f.write(resp.content)
                        return actor_local
                return None

            try:
                selected_actor_path = await asyncio.get_running_loop().run_in_executor(None, _download_actor)
            except Exception:
                pass
        else:
            src = os.path.join(OUTPUT_DIR, req.selected_actor_url.replace("/videos/", ""))
            if os.path.exists(src):
                selected_actor_path = src

    config = {
        "fal_key": fal_key,
        "elevenlabs_key": elevenlabs_key,
        "voice_id": req.voice_id or "21m00Tcm4TlvDq8ikWAM",
        "actor_description": req.actor_description,
        "selected_actor_path": selected_actor_path,
        "video_mode": req.video_mode,
    }

    async def run_generation():
        await concurrency_semaphore.acquire()
        try:
            loop = asyncio.get_running_loop()

            def log_msg(msg):
                print(f"[SaaSShorts Job {job_id[:8]}] {msg}")
                if job_id in saas_jobs:
                    saas_jobs[job_id]["logs"].append(msg)
                    _persist_saas_job(job_id)

            def run():
                return generate_full_video(req.script, config, job_output_dir, log_msg)

            result = await loop.run_in_executor(None, run)

            if job_id in saas_jobs:
                video_filename = result["video_filename"]
                saas_jobs[job_id]["status"] = "completed"
                saas_jobs[job_id]["result"] = {
                    "video_url": f"/videos/saas_{job_id}/{video_filename}",
                    "video_filename": video_filename,
                    "duration": result.get("duration", 0),
                    "cost_estimate": result.get("cost_estimate", {}),
                    "script": req.script,
                }
                saas_jobs[job_id]["logs"].append("Video generation completed!")
                _persist_saas_job(job_id)

                # Upload to public gallery (non-blocking)
                try:
                    gallery_meta = {
                        "title": req.script.get("title", "Untitled"),
                        "hook_text": req.script.get("hook_text", ""),
                        "caption": req.script.get("caption", ""),
                        "hashtags": req.script.get("hashtags", []),
                        "full_narration": req.script.get("full_narration", ""),
                        "actor_description": req.script.get("actor_description", ""),
                        "style": req.script.get("style", "ugc"),
                        "language": req.script.get("language", "en"),
                        "duration": result.get("duration", 0),
                        "video_mode": req.video_mode,
                        "product_name": req.script.get("_product_name", ""),
                        "product_url": req.script.get("_product_url", ""),
                        "segments": req.script.get("segments", []),
                        "cost_estimate": result.get("cost_estimate", {}),
                    }
                    gallery_result = upload_video_to_gallery(
                        video_path=result["video_path"],
                        actor_image_path=result.get("actor_image", ""),
                        metadata=gallery_meta,
                        video_id=job_id[:8],
                    )
                    if gallery_result:
                        saas_jobs[job_id]["result"]["gallery_video_id"] = gallery_result["video_id"]
                        log_msg("📤 Uploaded to public gallery.")
                except Exception as gallery_err:
                    log_msg(f"⚠️ Gallery upload skipped: {gallery_err}")

        except Exception as e:
            print(f"[SaaSShorts] ❌ Job {job_id} failed: {e}")
            if job_id in saas_jobs:
                saas_jobs[job_id]["status"] = "failed"
                saas_jobs[job_id]["logs"].append(f"Error: {str(e)}")
                _persist_saas_job(job_id)
        finally:
            concurrency_semaphore.release()

    asyncio.create_task(run_generation())

    return {"job_id": job_id, "status": "processing"}


@app.get("/api/saasshorts/status/{job_id}")
async def saasshorts_status(job_id: str):
    """Poll SaaSShorts job status."""
    if job_id not in saas_jobs:
        raise HTTPException(status_code=404, detail="SaaSShorts job not found")

    job = saas_jobs[job_id]
    return {
        "status": job["status"],
        "logs": job["logs"],
        "result": job.get("result"),
    }


@app.get("/api/saasshorts/voices")
async def saasshorts_voices(
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key"),
):
    """List available ElevenLabs voices."""
    if x_elevenlabs_key:
        try:
            loop = asyncio.get_event_loop()
            voices = await loop.run_in_executor(
                None, get_elevenlabs_voices, x_elevenlabs_key
            )
            if voices:
                return {"voices": voices, "source": "elevenlabs"}
        except Exception:
            pass

    # Fallback to default voices
    return {
        "voices": [
            {"voice_id": vid, "name": name, "category": "default"}
            for name, vid in DEFAULT_VOICES.items()
        ],
        "source": "defaults",
    }
