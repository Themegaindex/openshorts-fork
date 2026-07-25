import asyncio
import io
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

# These tests exercise the FastAPI backend directly. On dev machines without
# the full server stack installed, skip this module instead of aborting the
# whole suite at collection time (CI installs fastapi and runs everything).
pytest.importorskip("fastapi", reason="fastapi not installed — backend reliability tests run in CI")

import app


def _configure_aux_state(monkeypatch, tmp_path):
    thumbnail_dir = tmp_path / ".thumbnail_sessions"
    publish_dir = tmp_path / ".publish_jobs"
    thumbnail_dir.mkdir()
    publish_dir.mkdir()
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "THUMBNAIL_SESSION_STATE_DIR", str(thumbnail_dir))
    monkeypatch.setattr(app, "PUBLISH_JOB_STATE_DIR", str(publish_dir))
    monkeypatch.setattr(app, "thumbnail_sessions", {})
    monkeypatch.setattr(app, "publish_jobs", {})
    monkeypatch.setattr(app, "saas_jobs", {})


def test_clip_version_is_persisted_to_metadata_and_job_state(monkeypatch, tmp_path):
    job_id = "job-1"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    metadata_path = output_dir / "video_metadata.json"
    metadata_path.write_text(
        json.dumps({"shorts": [{"video_url": "/videos/job-1/original.mp4"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "completed",
            "output_dir": str(output_dir),
            "result": {"clips": [{"video_url": "/videos/job-1/original.mp4"}]},
            "raw_logs": [],
            "important_logs": [],
        }
    })

    new_url = "/videos/job-1/edited_unique_original.mp4"
    app._update_clip_version(job_id, 0, new_url, metadata_path=str(metadata_path))

    assert app.jobs[job_id]["result"]["clips"][0]["video_url"] == new_url
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["shorts"][0]["video_url"] == new_url
    persisted = json.loads((output_dir / app.JOB_STATE_FILENAME).read_text(encoding="utf-8"))
    assert persisted["result"]["clips"][0]["video_url"] == new_url


@pytest.mark.parametrize("output_format", ["vertical", "square", "original"])
def test_full_video_artifact_recovery_supports_every_canonical_format(tmp_path, output_format):
    filename = f"My_Show_{output_format}.mp4"
    (tmp_path / filename).write_bytes(b"video")

    result = app._build_result_from_video_artifacts("job-format", str(tmp_path))

    assert result is not None
    assert result["clips"][0]["output_filename"] == filename
    assert result["clips"][0]["video_url"] == f"/videos/job-format/{filename}"
    assert result["clips"][0]["video_title_for_youtube_short"] == "My Show"


@pytest.mark.parametrize(
    "model,payload",
    [
        (app.EditRequest, {"job_id": "j", "clip_index": -1}),
        (app.SubtitleRequest, {"job_id": "j", "clip_index": -1}),
        (app.HookRequest, {"job_id": "j", "clip_index": -1, "text": "hook"}),
        (app.TranslateRequest, {"job_id": "j", "clip_index": -1, "target_language": "de"}),
        (app.SocialPostRequest, {
            "job_id": "j", "clip_index": -1, "api_key": "k", "user_id": "u", "platforms": ["youtube"]
        }),
        (app.RemoveLayerRequest, {"job_id": "j", "clip_index": -1, "layer": "subtitle"}),
    ],
)
def test_negative_clip_indices_are_rejected(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("layer", ["subtitle", "hook", "all"])
def test_remove_layer_accepts_the_documented_layers(layer):
    assert app.RemoveLayerRequest(job_id="j", clip_index=0, layer=layer).layer == layer


@pytest.mark.parametrize("layer", ["", "edit", "watermark", "SUBTITLE"])
def test_remove_layer_rejects_unknown_layers(layer):
    with pytest.raises(ValidationError):
        app.RemoveLayerRequest(job_id="j", clip_index=0, layer=layer)


@pytest.mark.parametrize(
    "field,value",
    [
        ("position", "center-ish"),
        ("style", "animated"),
        ("preset", "disco_fire"),
        ("effect", "shake"),
        ("font_color", "red"),
        ("font_size", 500),
        ("bg_opacity", 1.5),
    ],
)
def test_invalid_subtitle_options_are_rejected(field, value):
    payload = {"job_id": "j", "clip_index": 0, field: value}
    with pytest.raises(ValidationError):
        app.SubtitleRequest(**payload)


def test_safe_bounce_is_a_valid_subtitle_effect():
    request = app.SubtitleRequest(job_id="j", clip_index=0, effect="bounce", style="karaoke")
    assert request.effect == "bounce"


def test_signature_subtitle_presets_are_valid():
    neon = app.SubtitleRequest(job_id="j", clip_index=0, preset="neon_sweep")
    rainbow = app.SubtitleRequest(job_id="j", clip_index=0, preset="rainbow_word")
    assert neon.preset == "neon_sweep"
    assert rainbow.preset == "rainbow_word"


def test_worker_summary_does_not_publish_completed_before_validation(monkeypatch, tmp_path):
    job_id = "job-summary"
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "processing",
            "output_dir": str(tmp_path),
            "raw_logs": [],
            "important_logs": [],
        }
    })

    app._apply_job_event(job_id, {
        "type": "summary",
        "status": "completed",
        "message": "worker done",
    })

    assert app.jobs[job_id]["status"] == "processing"
    assert app.jobs[job_id]["worker_summary_received"] is True


def test_legacy_completed_job_elapsed_time_is_frozen(monkeypatch):
    # The reported job was polled 799s after completion. Before the fix those
    # 799s were incorrectly added to its real runtime on every status request.
    monkeypatch.setattr(app, "_now_ts", lambda: 6119.0)
    payload = app._build_status_payload({
        "job_id": "legacy-complete",
        "status": "completed",
        "started_at": 1000.0,
        "updated_at": 5320.0,
        "last_heartbeat_at": 5314.0,
        "raw_logs": [],
    })

    assert payload["elapsed_seconds"] == 4320
    assert payload["actual_duration_seconds"] == 4320
    assert payload["finished_at"] == 5320.0


def test_mark_completed_persists_exact_duration_and_live_eta_state(monkeypatch, tmp_path):
    job_id = "timed-complete"
    monkeypatch.setattr(app, "_now_ts", lambda: 5320.0)
    monkeypatch.setattr(app, "_persist_job_state", lambda _job_id: None)
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "processing",
            "started_at": 1000.0,
            "output_dir": str(tmp_path),
        }
    })

    app._mark_job_status(job_id, "completed", resumable=False)
    job = app.jobs[job_id]

    assert job["finished_at"] == 5320.0
    assert job["actual_duration_seconds"] == 4320
    assert job["phase_eta_seconds"] == 0
    assert job["eta_state"] == "done"


def test_job_event_persists_phase_timing_fields(monkeypatch, tmp_path):
    job_id = "phase-timing"
    monkeypatch.setattr(app, "_persist_job_state", lambda _job_id: None)
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "processing",
            "output_dir": str(tmp_path),
            "raw_logs": [],
            "important_logs": [],
        }
    })

    app._apply_job_event(job_id, {
        "type": "progress",
        "timestamp": 2000.0,
        "phase_eta_seconds": 900,
        "eta_seconds": 900,
        "eta_state": "live",
        "phase_durations_seconds": {"download": 75.2, "transcribe": 3900.5},
    })

    job = app.jobs[job_id]
    assert job["phase_eta_seconds"] == 900
    assert job["eta_state"] == "live"
    assert job["phase_durations_seconds"]["transcribe"] == 3900.5


def test_run_job_validates_result_before_completed(monkeypatch, tmp_path):
    job_id = "job-order"
    execution_id = "execution-1"
    events = []

    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    job = {
        "job_id": job_id,
        "cmd": ["fake-worker"],
        "env": {},
        "execution_id": execution_id,
        "output_dir": str(tmp_path),
        "status": "queued",
        "raw_logs": [],
        "important_logs": [],
    }
    monkeypatch.setattr(app, "jobs", {job_id: job})
    monkeypatch.setattr(app.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(app, "_register_job_process", lambda *args: None)
    monkeypatch.setattr(app, "_unregister_job_process", lambda *args: None)
    monkeypatch.setattr(app, "_append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_persist_job_state", lambda *args: None)
    monkeypatch.setattr(app, "upload_job_artifacts", lambda *args: None)

    def mark(_job_id, status, **kwargs):
        events.append(("status", status))
        job["status"] = status

    def refresh(_job_id, _output_dir):
        events.append(("validate", "result"))
        return {"clips": [{"video_url": "/videos/job-order/clip.mp4"}]}

    monkeypatch.setattr(app, "_mark_job_status", mark)
    monkeypatch.setattr(app, "_refresh_job_result", refresh)

    asyncio.run(app.run_job(job_id, job))

    assert events.index(("validate", "result")) < events.index(("status", "completed"))


def test_resume_stops_old_process_and_uses_automatic_phase(monkeypatch, tmp_path):
    job_id = "job-resume"

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = FakeProcess()
    job = {
        "job_id": job_id,
        "status": "stalled",
        "is_resumable": True,
        "output_dir": str(tmp_path),
        "progress_percent": 50,
        "raw_logs": [],
        "important_logs": [],
        "finished_at": 123.0,
        "actual_duration_seconds": 100,
        "eta_state": "done",
        "phase_eta_seconds": 0,
        "phase_durations_seconds": {"transcribe": 90.0},
    }
    monkeypatch.setattr(app, "jobs", {job_id: job})
    monkeypatch.setattr(app, "job_processes", {job_id: {process}})

    async def run_resume():
        monkeypatch.setattr(app, "job_queue", asyncio.Queue())
        request = SimpleNamespace(headers={"X-Gemini-Key": "test-key"})
        return await app.resume_job(job_id, request, None)

    result = asyncio.run(run_resume())

    assert process.terminated is True
    assert result["status"] == "queued"
    assert "--resume-phase" not in job["cmd"]
    assert job["finished_at"] is None
    assert job["actual_duration_seconds"] is None
    assert job["eta_state"] == "calculating"
    assert job["phase_durations_seconds"] == {}


def test_auxiliary_jobs_recover_after_restart(monkeypatch, tmp_path):
    _configure_aux_state(monkeypatch, tmp_path)

    app.thumbnail_sessions["thumb-1"] = {
        "created_at": app._now_ts(),
        "transcript_ready": True,
        "transcript": {"text": "hello"},
        "transcript_event": asyncio.Event(),
    }
    app.publish_jobs["publish-1"] = {
        "created_at": app._now_ts(),
        "status": "uploading",
        "result": None,
        "error": None,
    }
    saas_dir = tmp_path / "saas_saas-1"
    saas_dir.mkdir()
    app.saas_jobs["saas-1"] = {
        "status": "processing",
        "logs": ["started"],
        "result": None,
        "output_dir": str(saas_dir),
    }
    app._persist_thumbnail_session("thumb-1")
    app._persist_publish_job("publish-1")
    app._persist_saas_job("saas-1")

    app.thumbnail_sessions.clear()
    app.publish_jobs.clear()
    app.saas_jobs.clear()
    app._recover_auxiliary_state()

    assert app.thumbnail_sessions["thumb-1"]["transcript"]["text"] == "hello"
    assert app.thumbnail_sessions["thumb-1"]["transcript_event"].is_set()
    assert app.publish_jobs["publish-1"]["status"] == "failed"
    assert app.saas_jobs["saas-1"]["status"] == "failed"
    assert "Retry" in app.saas_jobs["saas-1"]["logs"][-1]


def test_thumbnail_url_download_runs_off_event_loop(monkeypatch, tmp_path):
    _configure_aux_state(monkeypatch, tmp_path)
    main_thread_id = threading.get_ident()
    calls = {}
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")

    def fake_download(url, output_dir):
        calls["download_thread"] = threading.get_ident()
        return str(video_path), "video"

    fake_main = SimpleNamespace(download_youtube_video=fake_download)
    monkeypatch.setitem(sys.modules, "main", fake_main)
    monkeypatch.setattr(app, "analyze_video_for_titles", lambda *args: {
        "titles": ["Title"],
        "transcript_summary": "Summary",
        "language": "en",
        "recommended": [],
        "segments": [],
        "video_duration": 0,
    })
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path))

    result = asyncio.run(app.thumbnail_analyze(
        request=SimpleNamespace(),
        file=None,
        url="https://example.com/video",
        session_id=None,
        x_gemini_key="key",
    ))

    assert result["titles"] == ["Title"]
    assert calls["download_thread"] != main_thread_id


def test_clip_operation_lock_is_shared_per_clip(monkeypatch):
    monkeypatch.setattr(app, "clip_operation_locks", {})
    first = app._get_clip_operation_lock("job", 0)
    second = app._get_clip_operation_lock("job", 0)
    other = app._get_clip_operation_lock("job", 1)
    assert first is second
    assert first is not other


def test_edit_keeps_not_found_http_status(monkeypatch, tmp_path):
    job_id = "missing-edit-input"
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "result": {"clips": [{"video_url": f"/videos/{job_id}/missing.mp4"}]},
            "output_dir": str(tmp_path / job_id),
        }
    })
    request = app.EditRequest(job_id=job_id, clip_index=0)

    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(app._edit_clip_locked(request, "gemini-key"))

    assert exc.value.status_code == 404


def test_subtitle_keeps_bad_request_http_status(monkeypatch, tmp_path):
    job_id = "empty-subtitles"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    video_path = output_dir / "clip.mp4"
    video_path.write_bytes(b"placeholder")
    metadata_path = output_dir / "video_metadata.json"
    metadata_path.write_text(json.dumps({
        "transcript": {"segments": []},
        "shorts": [{"start": 0, "end": 10, "video_url": f"/videos/{job_id}/clip.mp4"}],
    }), encoding="utf-8")
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "result": {"clips": [{"video_url": f"/videos/{job_id}/clip.mp4"}]},
            "output_dir": str(output_dir),
        }
    })
    request = app.SubtitleRequest(job_id=job_id, clip_index=0)

    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(app._add_subtitles_locked(request))

    assert exc.value.status_code == 400
