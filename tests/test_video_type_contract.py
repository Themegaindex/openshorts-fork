import asyncio
import json
import os
from pathlib import Path
import zipfile

import pytest

pytest.importorskip("fastapi", reason="FastAPI contract tests run in the server environment")

from fastapi import HTTPException
from starlette.requests import Request

import app


def _write_metadata(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_result_builder_appends_ready_long_video_after_shorts(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"short")
    (tmp_path / "long.mp4").write_bytes(b"long")
    metadata_path = tmp_path / "show_metadata.json"
    _write_metadata(metadata_path, {
        "video_type": "auto",
        "processing_mode": "clips_and_long",
        "shorts": [{"output_filename": "clip.mp4", "start": 10, "end": 40}],
        "long_videos": [{
            "output_filename": "long.mp4", "start": 0, "end": 540,
            "title": "Long title", "chapters": [],
        }],
    })

    result = app._build_result_from_metadata("job", str(metadata_path), str(tmp_path))

    assert [item["output_filename"] for item in result["clips"]] == ["clip.mp4", "long.mp4"]
    assert result["clips"][1]["video_type"] == "long"
    assert result["video_type"] == "auto"
    assert result["processing_mode"] == "clips_and_long"


def test_combined_index_updates_long_metadata_and_survives_a_filtered_short(monkeypatch, tmp_path):
    job_id = "combined-job"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    metadata_path = output_dir / "show_metadata.json"
    _write_metadata(metadata_path, {
        "shorts": [
            {"output_filename": "missing.mp4"},
            {"output_filename": "ready.mp4"},
        ],
        "long_videos": [{"output_filename": "long.mp4", "video_type": "long"}],
    })
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "completed",
            "output_dir": str(output_dir),
            "result": {"clips": [
                {"output_filename": "ready.mp4", "video_url": f"/videos/{job_id}/ready.mp4"},
                {"output_filename": "long.mp4", "video_url": f"/videos/{job_id}/long.mp4", "video_type": "long"},
            ]},
            "raw_logs": [],
            "important_logs": [],
        },
    })

    app._update_clip_version(
        job_id, 1, f"/videos/{job_id}/translated_long.mp4", metadata_path=str(metadata_path),
    )

    saved = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "video_url" not in saved["shorts"][0]
    assert "video_url" not in saved["shorts"][1]
    assert saved["long_videos"][0]["video_url"].endswith("translated_long.mp4")


def test_video_type_is_in_persisted_job_state():
    serialized = app._serialize_job({
        "job_id": "job", "status": "queued", "video_type": "auto",
    })
    assert serialized["video_type"] == "auto"


def test_download_all_uses_output_filename_for_untouched_short_and_long(monkeypatch, tmp_path):
    job_id = "zip-job"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    (output_dir / "short.mp4").write_bytes(b"short")
    (output_dir / "long.mp4").write_bytes(b"long")
    _write_metadata(output_dir / "show_metadata.json", {
        "shorts": [{"output_filename": "short.mp4"}],
        "long_videos": [{"output_filename": "long.mp4"}],
    })
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))

    response = asyncio.run(app.download_all_clips(job_id))

    with zipfile.ZipFile(response.path) as archive:
        assert archive.namelist() == ["clip_01_short.mp4", "long_01_long.mp4"]


def test_single_clip_download_streams_the_current_long_file(monkeypatch, tmp_path):
    job_id = "download-long"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    video_path = output_dir / "long.mp4"
    video_path.write_bytes(b"long video")
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "output_dir": str(output_dir),
            "result": {"clips": [{
                "video_url": f"/videos/{job_id}/long.mp4",
                "video_type": "long",
            }]},
        },
    })

    response = asyncio.run(app.download_clip(job_id, 0))

    assert response.path == str(video_path)
    assert response.headers["content-disposition"].startswith("attachment;")
    result_card = (
        Path(__file__).parents[1] / "dashboard" / "src" / "components" / "ResultCard.jsx"
    ).read_text(encoding="utf-8")
    assert "response.blob()" not in result_card
    assert "/clips/${index}/download" in result_card


def test_social_post_rejects_long_before_vendor_request(monkeypatch):
    job_id = "long-social"
    monkeypatch.setattr(app, "jobs", {
        job_id: {"result": {"clips": [{"video_type": "long", "video_url": "/videos/x/long.mp4"}]}}
    })
    request = app.SocialPostRequest(
        job_id=job_id,
        clip_index=0,
        api_key="key",
        user_id="user",
        platforms=["youtube"],
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(app.post_to_socials(request))
    assert exc_info.value.status_code == 400
    assert "long videos" in exc_info.value.detail


class _Queue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


def _json_request(payload, api_key="gemini-key", extra_headers=()):
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/process",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-gemini-key", api_key.encode("utf-8")),
            *extra_headers,
        ],
    }, receive)


@pytest.mark.parametrize("video_type,expected_arg", [("auto", True), ("shorts", False), ("invalid", False)])
def test_process_endpoint_forwards_only_nonlegacy_video_type(
    monkeypatch, tmp_path, video_type, expected_arg,
):
    queue = _Queue()
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "QUALITY_GATE_MIN_HEIGHT", 0)
    monkeypatch.setattr(app, "jobs", {})
    monkeypatch.setattr(app, "job_queue", queue)
    request = _json_request({
        "url": "https://example.com/video",
        "output_format": "vertical",
        "layout_style": "smart",
        "video_type": video_type,
    })

    response = asyncio.run(app.process_endpoint(
        request, file=None, url=None, output_format=None, layout_style=None, video_type=None,
    ))
    command = app.jobs[response["job_id"]]["cmd"]

    assert ("--video-type" in command) is expected_arg
    if expected_arg:
        assert command[command.index("--video-type") + 1] == video_type
    else:
        assert app.jobs[response["job_id"]]["video_type"] == "shorts"


def test_translate_can_resolve_and_commit_a_long_video(monkeypatch, tmp_path):
    job_id = "translate-long"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    source = output_dir / "long.mp4"
    source.write_bytes(b"video")
    metadata_path = output_dir / "show_metadata.json"
    _write_metadata(metadata_path, {
        "shorts": [],
        "long_videos": [{"output_filename": "long.mp4", "video_type": "long"}],
    })
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "completed",
            "output_dir": str(output_dir),
            "result": {"clips": [{
                "output_filename": "long.mp4",
                "video_url": f"/videos/{job_id}/long.mp4",
                "video_type": "long",
            }]},
            "raw_logs": [],
            "important_logs": [],
        },
    })

    def fake_translate(*, video_path, output_path, **_kwargs):
        assert os.path.basename(video_path) == "long.mp4"
        with open(output_path, "wb") as output:
            output.write(b"translated")

    monkeypatch.setattr(app, "translate_video", fake_translate)
    response = asyncio.run(app._translate_clip_locked(
        app.TranslateRequest(job_id=job_id, clip_index=0, target_language="de"),
        "elevenlabs-key",
    ))

    assert response["success"] is True
    saved = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert saved["long_videos"][0]["video_url"].startswith(f"/videos/{job_id}/translated_de_")


def test_subtitle_can_remap_and_remove_a_long_video_layer(monkeypatch, tmp_path):
    job_id = "subtitle-long"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    (output_dir / "long.mp4").write_bytes(b"video")
    metadata_path = output_dir / "show_metadata.json"
    _write_metadata(metadata_path, {
        "transcript": {"segments": [{"words": [
            {"word": " First", "start": 10.2, "end": 10.4},
            {"word": " Second", "start": 20.3, "end": 20.5},
        ]}]},
        "shorts": [],
        "long_videos": [{
            "output_filename": "long.mp4",
            "video_type": "long",
            "start": 0,
            "end": 2,
            "segments": [
                {"start": 10, "end": 11},
                {"start": 20, "end": 21},
            ],
        }],
    })
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "completed",
            "output_dir": str(output_dir),
            "result": {"clips": [{
                "output_filename": "long.mp4",
                "video_url": f"/videos/{job_id}/long.mp4",
                "video_type": "long",
            }]},
            "raw_logs": [],
            "important_logs": [],
        },
    })
    captured = {}

    def fake_generate_srt(transcript, start, end, output_path):
        captured["transcript"] = transcript
        captured["range"] = (start, end)
        Path(output_path).write_text("subtitle", encoding="utf-8")
        return True

    def fake_render(_output_dir, _entry, output_path):
        Path(output_path).write_bytes(b"subtitled video")

    monkeypatch.setattr(app, "generate_srt", fake_generate_srt)
    monkeypatch.setattr(app, "_render_stored_layers", fake_render)

    response = asyncio.run(app._add_subtitles_locked(
        app.SubtitleRequest(job_id=job_id, clip_index=0),
    ))

    words = [
        word
        for segment in captured["transcript"]["segments"]
        for word in segment["words"]
    ]
    assert response["success"] is True
    assert captured["range"] == (0, 2.0)
    assert [word["start"] for word in words] == pytest.approx([0.2, 1.3])

    removed = asyncio.run(app._remove_clip_layer_locked(
        app.RemoveLayerRequest(job_id=job_id, clip_index=0, layer="subtitle"),
    ))
    assert removed["new_video_url"] == f"/videos/{job_id}/long.mp4"
    assert removed["layers"] == {"subtitle": False, "hook": False}

    saved = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert saved["long_videos"][0]["video_url"] == f"/videos/{job_id}/long.mp4"


def test_long_result_card_offers_subtitles_instead_of_dubbing():
    result_card = (
        Path(__file__).parents[1] / "dashboard" / "src" / "components" / "ResultCard.jsx"
    ).read_text(encoding="utf-8")
    actions = result_card.split("{/* Actions Footer */}", 1)[1]
    long_actions = actions.split("{isLong ? (", 1)[1].split(") : (", 1)[0]
    assert "setShowSubtitleModal(true)" in long_actions
    assert "Dub Voice" not in long_actions


def _process_with_request_id(monkeypatch, tmp_path, queue, request_id):
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "QUALITY_GATE_MIN_HEIGHT", 0)
    monkeypatch.setattr(app, "job_queue", queue)
    request = _json_request(
        {"url": "https://example.com/video", "output_format": "vertical",
         "layout_style": "smart", "video_type": "shorts"},
        extra_headers=[(b"x-process-request-id", request_id.encode())],
    )
    return asyncio.run(app.process_endpoint(
        request, file=None, url=None, output_format=None, layout_style=None, video_type=None,
    ))


def test_stop_before_process_answers_never_enqueues_the_job(monkeypatch, tmp_path):
    """"Stop & New" during the upload/quality check: the cancel arrives before
    the job exists, so the job must never be created."""
    queue = _Queue()
    monkeypatch.setattr(app, "jobs", {})
    monkeypatch.setattr(app, "_process_requests", {})
    request_id = "req_" + "a" * 12

    cancelled = asyncio.run(app.cancel_process_request(request_id))
    assert cancelled["job_id"] is None and cancelled["success"] is True

    response = _process_with_request_id(monkeypatch, tmp_path, queue, request_id)

    assert response == {"job_id": None, "status": "cancelled"}
    assert queue.items == [] and app.jobs == {}
    assert list(tmp_path.iterdir()) == []  # job directory cleaned up


def test_stop_after_process_answered_cancels_the_created_job(monkeypatch, tmp_path):
    queue = _Queue()
    monkeypatch.setattr(app, "jobs", {})
    monkeypatch.setattr(app, "_process_requests", {})
    monkeypatch.setattr(app, "_terminate_job_processes", lambda job_id: None)
    request_id = "req_" + "b" * 12

    response = _process_with_request_id(monkeypatch, tmp_path, queue, request_id)
    job_id = response["job_id"]
    assert queue.items == [job_id]

    cancelled = asyncio.run(app.cancel_process_request(request_id))

    assert cancelled == {"job_id": job_id, "success": True}
    assert app.jobs[job_id]["status"] == "failed"
    assert app.jobs[job_id]["cancel_requested"] is True


def test_cancel_process_request_rejects_malformed_ids():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.cancel_process_request("../etc"))
    assert exc.value.status_code == 400


def test_request_registry_keeps_active_jobs_and_caps_pending_entries(monkeypatch):
    now = app._now_ts()
    old = now - app.PROCESS_REQUEST_TTL_SECONDS - 1
    monkeypatch.setattr(app, "jobs", {
        "queued-job": {"status": "queued"},
        "done-job": {"status": "completed"},
    })
    monkeypatch.setattr(app, "PROCESS_REQUEST_MAX_PENDING", 2)
    registry = {
        "active_" + "a" * 8: {"job_id": "queued-job", "cancelled": False, "ts": old},
        "finished" + "b" * 8: {"job_id": "done-job", "cancelled": False, "ts": old},
        "stale___" + "c" * 8: {"job_id": None, "cancelled": True, "ts": old},
        "pending1" + "d" * 8: {"job_id": None, "cancelled": False, "ts": now - 3},
        "pending2" + "e" * 8: {"job_id": None, "cancelled": False, "ts": now - 2},
        "pending3" + "f" * 8: {"job_id": None, "cancelled": False, "ts": now - 1},
    }
    monkeypatch.setattr(app, "_process_requests", registry)

    app._prune_process_requests(now)

    # The queued job outlives the TTL; finished and stale entries are gone;
    # only the newest pending entries survive the cap.
    assert set(registry) == {"active_" + "a" * 8, "pending2" + "e" * 8, "pending3" + "f" * 8}

    # Cancelling the long-queued job by request id still reaches the job.
    monkeypatch.setattr(app, "_terminate_job_processes", lambda job_id: None)
    app.jobs["queued-job"].update({"job_id": "queued-job", "logs": [], "raw_logs": []})
    monkeypatch.setattr(app, "_mark_job_status", lambda job_id, status, **kw: app.jobs[job_id].update(status=status))
    monkeypatch.setattr(app, "_append_log", lambda *a, **k: None)
    result = asyncio.run(app.cancel_process_request("active_" + "a" * 8))
    assert result == {"job_id": "queued-job", "success": True}
    assert app.jobs["queued-job"]["status"] == "failed"
