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


def test_live_transcription_eta_waits_for_real_job_progress(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    reporter = main.JobReporter(job_id="eta-live")
    reporter.phase = "transcribe"
    reporter.phase_started_at = 1000.0

    clock[0] = 1119.0
    reporter.phase_progress_percent = 20.0
    assert reporter._estimate_live_phase_seconds() is None

    clock[0] = 1120.0
    reporter.phase_progress_percent = 9.9
    assert reporter._estimate_live_phase_seconds() is None

    reporter.phase_progress_percent = 10.0
    assert reporter._estimate_live_phase_seconds() == 1080


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
