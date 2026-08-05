import pytest

cv2 = pytest.importorskip("cv2", reason="OpenCV integration tests run in the video environment")
np = pytest.importorskip("numpy")
pytest.importorskip("ultralytics")
pytest.importorskip("mediapipe")

import main
from video_formats import EVEN_PAD_FILTER


class _FrameNumber:
    def __init__(self, value):
        self.value = value

    def get_frames(self):
        return self.value


@pytest.mark.parametrize(
    "source_shape,output_size",
    [
        ((1080, 1920, 3), (608, 1080)),   # landscape -> vertical
        ((1280, 720, 3), (720, 720)),     # portrait -> square
        ((1080, 1080, 3), (608, 1080)),   # square -> vertical
    ],
)
def test_general_frame_handles_every_source_orientation(source_shape, output_size):
    frame = np.full(source_shape, 127, dtype=np.uint8)
    output_width, output_height = output_size
    rendered = main.create_general_frame(frame, output_width, output_height)
    assert rendered.shape == (output_height, output_width, 3)


def test_portrait_to_square_crop_tracks_both_axes_without_stretching():
    camera = main.SmoothedCameraman(720, 720, 720, 1280, aspect_ratio=1.0)
    camera.update_target((200, 900, 200, 200))
    x1, y1, x2, y2 = camera.get_crop_box(force_snap=True)
    assert (x2 - x1, y2 - y1) == (720, 720)
    assert y1 > 0
    assert y2 <= 1280


def test_landscape_to_vertical_crop_keeps_target_aspect():
    camera = main.SmoothedCameraman(608, 1080, 1920, 1080, aspect_ratio=9 / 16)
    x1, y1, x2, y2 = camera.get_crop_box()
    assert (x2 - x1, y2 - y1) == (608, 1080)


@pytest.mark.parametrize("format_name", ["original", "horizontal"])
def test_original_and_legacy_horizontal_use_passthrough(monkeypatch, format_name):
    calls = []
    monkeypatch.setattr(main, "_finalize_clip_passthrough", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(main, "process_video_to_vertical", lambda *args, **kwargs: False)
    assert main._render_clip("in.mp4", "out.mp4", output_format=format_name) is True
    assert len(calls) == 1


def test_legacy_auto_is_explicit_vertical(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "process_video_to_vertical", lambda *args, **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr(main, "_finalize_clip_passthrough", lambda *args: False)
    assert main._render_clip("in.mp4", "out.mp4", output_format="auto") is True
    assert calls[0]["aspect_ratio"] == pytest.approx(9 / 16)


def test_wide_layout_skips_video_and_detector_analysis(monkeypatch):
    monkeypatch.setattr(
        main.cv2,
        "VideoCapture",
        lambda *_args: pytest.fail("wide mode must not open the source for layout analysis"),
    )
    decisions = main.analyze_scenes_strategy(
        "unused.mp4",
        [(_FrameNumber(0), _FrameNumber(30)), (_FrameNumber(30), _FrameNumber(60))],
        layout_style="wide",
    )
    assert [decision.strategy for decision in decisions] == ["GENERAL", "GENERAL"]


def test_zoom_layout_skips_yolo_person_sampling(monkeypatch):
    class FakeCapture:
        def isOpened(self):
            return True

        def get(self, property_id):
            if property_id == main.cv2.CAP_PROP_FRAME_WIDTH:
                return 1920
            if property_id == main.cv2.CAP_PROP_FPS:
                return 30
            return 0

        def set(self, *_args):
            return True

        def read(self):
            return True, np.zeros((32, 32, 3), dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(main.cv2, "VideoCapture", lambda *_args: FakeCapture())
    monkeypatch.setattr(main, "sample_scene_frames", lambda *_args, **_kwargs: [0])
    monkeypatch.setattr(main, "detect_face_candidates", lambda _frame: [])
    monkeypatch.setattr(
        main,
        "detect_person_boxes",
        lambda _frame: pytest.fail("zoom mode must not run YOLO person sampling"),
    )

    decisions = main.analyze_scenes_strategy(
        "unused.mp4",
        [(_FrameNumber(0), _FrameNumber(30))],
        layout_style="zoom",
    )
    assert decisions[0].strategy == "GENERAL"


def test_watermarked_passthrough_pads_odd_dimensions(monkeypatch, tmp_path):
    commands = []

    class FakeWatermark:
        def save(self, path):
            with open(path, "wb") as file_handle:
                file_handle.write(b"png")

    monkeypatch.setattr(main, "WATERMARK_ENABLED", True)
    monkeypatch.setattr(main, "get_video_resolution", lambda _path: (641, 359))
    monkeypatch.setattr(main, "_render_watermark_rgba", lambda _width: FakeWatermark())
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )

    assert main._finalize_clip_passthrough(
        str(tmp_path / "odd.mp4"), str(tmp_path / "odd_out.mp4"),
    ) is True
    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert EVEN_PAD_FILTER in filter_complex


def test_transcription_eta_blends_prior_with_live_progress(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(tmp_path / ".job_stats.json"))
    reporter = main.JobReporter(job_id="eta-live")
    reporter.phase = "transcribe"
    reporter.phase_started_at = 1000.0
    reporter.video_duration = 600.0

    assert reporter._estimate_phase_remaining() == 300.0

    clock[0] = 1010.0
    reporter.phase_progress_percent = 10.0
    assert reporter._estimate_phase_remaining() == pytest.approx(252.0)


def test_operation_deadline_moves_only_with_real_work(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(tmp_path / ".job_stats.json"))
    reporter = main.JobReporter(job_id="operation-watchdog")

    reporter.begin_operation("ffmpeg", timeout_seconds=120, expected_seconds=60)
    assert reporter.operation_deadline_at == 1120.0
    assert reporter._estimate_phase_remaining() is None  # queued is not an ETA phase

    clock[0] = 1040.0
    reporter.phase = "download"
    reporter.phase_started_at = 1000.0
    reporter.progress(50.0, message="made progress")
    assert reporter.operation_deadline_at == 1160.0
    assert reporter._estimate_phase_remaining() == pytest.approx(20.0)

    clock[0] = 1050.0
    reporter.heartbeat("keepalive", force=True)
    assert reporter.operation_deadline_at == 1160.0


def test_nested_operation_restores_parent_with_fresh_deadline(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(tmp_path / ".job_stats.json"))
    reporter = main.JobReporter(job_id="nested-watchdog")

    reporter.begin_operation("Gemini analysis", timeout_seconds=1800)
    assert reporter.operation_deadline_at == 2800.0

    clock[0] = 1500.0
    with pytest.raises(TimeoutError):
        with reporter.operation("Gemini score attempt", timeout_seconds=630):
            assert reporter.operation_name == "Gemini score attempt"
            assert reporter.operation_deadline_at == 2130.0

            clock[0] = 2100.0
            reporter.heartbeat("keepalive", force=True)
            assert reporter.operation_deadline_at == 2130.0
            raise TimeoutError("request timed out")

    assert reporter.operation_name == "Gemini analysis"
    assert reporter.operation_timeout_seconds == 1800.0
    assert reporter.operation_deadline_at == 3900.0
    assert reporter.last_work_activity_at == 2100.0


def test_gemini_worker_has_request_scoped_watchdog(monkeypatch):
    operations = []
    worker_calls = []

    class Operation:
        def __enter__(self):
            operations[-1]["entered"] = True

        def __exit__(self, exc_type, exc_value, traceback):
            operations[-1]["exited"] = True

    class Reporter:
        def operation(self, name, **kwargs):
            operations.append({"name": name, **kwargs})
            return Operation()

    def fake_worker(mode, payload, **kwargs):
        worker_calls.append((mode, payload, kwargs))
        return {"payload": {"windows": []}}

    monkeypatch.setattr(main, "JOB_REPORTER", Reporter())
    monkeypatch.setattr(main, "_run_gemini_worker", fake_worker)

    result = main._call_gemini_worker(
        "score",
        {"windows": []},
        output_dir=".",
        video_title="watchdog",
        strategy="structured-schema",
        batch_index=1,
        total_batches=3,
        attempt=2,
        timeout_seconds=123,
        artifact_suffix="rescue_window_1",
    )

    assert result == {"payload": {"windows": []}}
    assert operations == [{
        "name": "Gemini score batch 2/3 attempt 2",
        "timeout_seconds": 123 + main.GEMINI_REQUEST_WATCHDOG_GRACE_SECONDS,
        "message": "Starting Gemini score batch 2/3 attempt 2.",
        "category": "gemini",
        "attempt": 2,
        "batch_index": 2,
        "total_batches": 3,
        "entered": True,
        "exited": True,
    }]
    assert worker_calls[0][2]["timeout_seconds"] == 123.0
    assert worker_calls[0][2]["artifact_suffix"] == "rescue_window_1"


def test_ytdlp_hooks_reserve_progress_and_eta_for_merge(monkeypatch):
    calls = []

    class Reporter:
        video_duration = 3600.0

        def progress(self, percent, **kwargs):
            calls.append(("progress", percent, kwargs))

        def begin_operation(self, name, **kwargs):
            calls.append(("begin", name, kwargs))

        def finish_operation(self, **kwargs):
            calls.append(("finish", None, kwargs))

        def heartbeat(self, *args, **kwargs):
            calls.append(("heartbeat", args, kwargs))

    monkeypatch.setattr(main, "JOB_REPORTER", Reporter())
    monkeypatch.setattr(main.time, "time", lambda: 1000.0)
    progress_hook, postprocessor_hook = main._make_ytdlp_progress_hooks()

    progress_hook({
        "status": "downloading",
        "total_bytes": 100,
        "downloaded_bytes": 100,
        "eta": 10,
    })
    network_progress = calls[-1]
    assert network_progress[0:2] == ("progress", 98.0)
    assert network_progress[2]["eta_seconds"] > 10
    assert network_progress[2]["eta_is_estimated"] is True

    postprocessor_hook({"status": "started", "postprocessor": "FFmpegMerger"})
    begin = next(call for call in calls if call[0] == "begin")
    merge_progress = calls[-1]
    assert begin[1] == "yt-dlp:FFmpegMerger"
    assert begin[2]["timeout_seconds"] >= 240
    assert merge_progress[0:2] == ("progress", 99.0)
    assert merge_progress[2]["phase_eta_seconds"] > 0
    assert merge_progress[2]["eta_is_estimated"] is True

    postprocessor_hook({"status": "finished", "postprocessor": "FFmpegMerger"})
    assert calls[-1][0] == "finish"


def test_resume_source_rejects_yt_dlp_fragments_and_partial_merge(monkeypatch, tmp_path):
    fragment = tmp_path / "Title.f625.mp4"
    partial_merge = tmp_path / "Title.temp.mp4"
    silent_final = tmp_path / "Silent.mp4"
    complete_final = tmp_path / "Complete.mp4"
    for path, size in ((fragment, 500), (partial_merge, 400), (silent_final, 300), (complete_final, 200)):
        path.write_bytes(b"x" * size)

    stream_map = {
        str(fragment): {"video"},
        str(partial_merge): {"video", "audio"},
        str(silent_final): {"video"},
        str(complete_final): {"video", "audio"},
    }
    monkeypatch.setattr(main, "_probe_stream_types", lambda path: stream_map[str(path)])

    assert main._find_source_video(str(tmp_path), require_audio=True) == str(complete_final)
    complete_final.unlink()
    assert main._find_source_video(str(tmp_path), require_audio=True) is None

    (tmp_path / "job_state.json").write_text(
        '{"source_url": "https://www.youtube.com/watch?v=test"}',
        encoding="utf-8",
    )
    context = main._load_resume_context(str(tmp_path))
    assert context["input_video"] is None
    assert context["source_url"].startswith("https://www.youtube.com/")

    removed = main._clean_partial_download(str(tmp_path))
    assert "Title.temp.mp4" in removed
    assert partial_merge.exists() is False
    assert fragment.exists() is True


def test_analysis_learning_subtracts_fixed_overhead_and_serializes_writers(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    stats_path = tmp_path / ".job_stats.json"
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(stats_path))
    reporters = []
    for index in range(6):
        reporter = main.JobReporter(job_id=f"stats-{index}")
        reporter.video_duration = 60.0
        reporter.phase_durations = {"analyze": 35.0 + index}
        reporters.append(reporter)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda reporter: reporter._record_job_stats(), reporters))

    stats = main._load_job_stats()
    unit = main.PHASE_COST_UNITS["analyze"]
    assert unit == "per_source_second_after_overhead"
    assert len(stats["analyze"][unit]) == 6
    # Fixed 30s overhead is removed before normalising by source duration.
    assert min(stats["analyze"][unit]) == pytest.approx(5.0 / 60.0, abs=0.0001)

    long_job = main.JobReporter(job_id="long-analysis")
    long_job.video_duration = 7200.0
    assert long_job._phase_cost_prior("analyze") < 1000.0


def test_job_stats_lock_is_exclusive_reusable_and_keeps_its_sidecar(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(tmp_path / ".job_stats.json"))

    with main._job_stats_file_lock():
        # A second holder must time out while the OS lock is held — even in
        # the same process, because every acquisition opens its own descriptor.
        with pytest.raises(TimeoutError):
            with main._job_stats_file_lock(timeout_seconds=0.2):
                pass

    # Released cleanly: the very next acquisition succeeds immediately.
    with main._job_stats_file_lock(timeout_seconds=0.2):
        pass

    # The sidecar file must survive on purpose. Deleting a locked path would
    # let the next process lock a fresh file while the old holder still owns
    # the removed one — the exact double-holder race the OS lock prevents.
    assert (tmp_path / ".job_stats.json.lock").exists()


def test_blocked_gemini_batch_rescues_each_window_once(monkeypatch):
    calls = []

    def fake_worker(mode, payload, **kwargs):
        window_id = payload["windows"][0]["id"]
        calls.append((mode, window_id, kwargs["artifact_suffix"]))
        if window_id == "blocked":
            raise main.GeminiWorkerError(
                "blocked",
                {"error_type": "blocked_response", "cost_analysis": {"total_cost": 0.01}},
            )
        return {
            "payload": {"windows": [{
                "id": window_id,
                "start": 0,
                "end": 90,
                "score": 90,
                "reason": "strong",
            }]},
            "cost_analysis": {"total_cost": 0.02},
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    windows = [
        {"id": "good-a", "start": 0, "end": 90, "text": "a"},
        {"id": "blocked", "start": 90, "end": 180, "text": "b"},
        {"id": "good-c", "start": 180, "end": 270, "text": "c"},
    ]

    successes, failures, costs, attempts = main._rescue_gemini_windows(
        "score",
        windows,
        video_duration=270,
        language="de",
        output_dir=".",
        video_title="test",
        batch_index=0,
        total_batches=1,
    )

    assert [window["id"] for window, _ in successes] == ["good-a", "good-c"]
    assert failures == ["blocked"]
    assert len(calls) == 3
    assert len(costs) == 3
    assert [attempt["status"] for attempt in attempts] == ["success", "failed", "success"]


def test_null_detail_rescue_payload_becomes_window_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "GEMINI_WORKER_SCRIPT", __file__)
    monkeypatch.setattr(main, "_build_transcript_windows", lambda *_args, **_kwargs: [{
        "id": "window_001",
        "start": 0.0,
        "end": 90.0,
        "text": "Test transcript",
    }])

    def fake_worker(mode, payload, **kwargs):
        if mode == "score":
            return {
                "payload": {"windows": [{
                    "id": "window_001",
                    "start": 0.0,
                    "end": 90.0,
                    "score": 90,
                    "reason": "strong",
                }]},
                "cost_analysis": None,
            }
        if kwargs.get("artifact_suffix"):
            return {"payload": None, "cost_analysis": None}
        raise main.GeminiWorkerError(
            "empty detail response",
            {"error_type": "empty_response", "payload": None},
        )

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_viral_clips(
        {"language": "de", "segments": [], "text": "Test transcript"},
        90.0,
        output_dir=str(tmp_path),
        video_title="null_payload",
    )

    assert result["clips_data"] is None
    assert result["analysis_coverage"]["detail_windows_processed"] == 0
    assert result["analysis_coverage"]["detail_windows_skipped"] == ["window_001"]
