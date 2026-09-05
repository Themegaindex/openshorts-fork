from contextlib import contextmanager
import copy
import importlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


_PIPELINE_DEPENDENCY_PREFIXES = (
    "cv2",
    "google",
    "mediapipe",
    "numpy",
    "scenedetect",
    "torch",
    "tqdm",
    "ultralytics",
    "yt_dlp",
)


def _is_pipeline_dependency_module(module_name):
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in _PIPELINE_DEPENDENCY_PREFIXES
    )


def _stub_missing_pipeline_dependencies():
    """Let dependency-free pipeline tests collect in lightweight CI.

    The functions in this module mock every renderer/model boundary they touch,
    so importing multi-gigabyte video/ML packages would add cost without adding
    coverage. Model packages are always stubbed because their constructors can
    download weights even after a successful package import.
    """
    def unavailable(name):
        try:
            importlib.import_module(name)
            return False
        except Exception:
            # A binary package can have discoverable metadata but still fail to
            # load (for example cv2 without libGL). Clear partial imports before
            # installing the test double that main.py will consume.
            for module_name in tuple(sys.modules):
                if module_name == name or module_name.startswith(f"{name}."):
                    sys.modules.pop(module_name, None)
            return True

    if unavailable("cv2"):
        sys.modules["cv2"] = MagicMock()
    if unavailable("numpy"):
        # Every numpy-using renderer boundary must stay mocked in this module.
        numpy_stub = ModuleType("numpy")
        numpy_stub._openshorts_test_stub = True
        sys.modules["numpy"] = numpy_stub
    if unavailable("torch"):
        torch_stub = ModuleType("torch")
        torch_stub.cuda = SimpleNamespace(is_available=lambda: False)
        sys.modules["torch"] = torch_stub
    if unavailable("tqdm"):
        tqdm_stub = ModuleType("tqdm")
        tqdm_stub.tqdm = lambda iterable, *_args, **_kwargs: iterable
        sys.modules["tqdm"] = tqdm_stub
    if unavailable("yt_dlp"):
        yt_dlp_stub = ModuleType("yt_dlp")
        yt_dlp_stub.YoutubeDL = MagicMock()
        yt_dlp_stub.version = SimpleNamespace(__version__="test-stub")
        sys.modules["yt_dlp"] = yt_dlp_stub
    if unavailable("scenedetect"):
        scene_stub = ModuleType("scenedetect")
        scene_stub.__path__ = []
        scene_stub.SceneManager = MagicMock
        scene_stub.VideoManager = MagicMock
        scene_stub.open_video = MagicMock()
        detectors_stub = ModuleType("scenedetect.detectors")
        detectors_stub.ContentDetector = MagicMock
        sys.modules["scenedetect"] = scene_stub
        sys.modules["scenedetect.detectors"] = detectors_stub
    ultralytics_stub = ModuleType("ultralytics")
    ultralytics_stub.YOLO = MagicMock
    sys.modules["ultralytics"] = ultralytics_stub
    mediapipe_stub = ModuleType("mediapipe")
    mediapipe_stub.__path__ = []
    mediapipe_stub.Image = MagicMock
    mediapipe_stub.ImageFormat = SimpleNamespace(SRGB="SRGB")
    tasks_stub = ModuleType("mediapipe.tasks")
    tasks_stub.__path__ = []
    python_stub = ModuleType("mediapipe.tasks.python")
    python_stub.__path__ = []
    python_stub.BaseOptions = MagicMock
    vision_stub = ModuleType("mediapipe.tasks.python.vision")
    vision_stub.FaceDetectorOptions = MagicMock
    vision_stub.RunningMode = SimpleNamespace(IMAGE="IMAGE")
    vision_stub.FaceDetector = SimpleNamespace(
        create_from_options=MagicMock(return_value=MagicMock()),
    )
    tasks_stub.python = python_stub
    python_stub.vision = vision_stub
    mediapipe_stub.tasks = tasks_stub
    sys.modules["mediapipe"] = mediapipe_stub
    sys.modules["mediapipe.tasks"] = tasks_stub
    sys.modules["mediapipe.tasks.python"] = python_stub
    sys.modules["mediapipe.tasks.python.vision"] = vision_stub
    if unavailable("google.genai"):
        if unavailable("google"):
            google_stub = ModuleType("google")
            google_stub.__path__ = []
            sys.modules["google"] = google_stub
        else:
            import google as google_stub
        genai_stub = ModuleType("google.genai")
        genai_stub.__path__ = []
        genai_stub.Client = MagicMock
        genai_types_stub = ModuleType("google.genai.types")
        genai_types_stub.GenerateContentConfig = MagicMock
        genai_stub.types = genai_types_stub
        google_stub.genai = genai_stub
        sys.modules["google.genai"] = genai_stub
        sys.modules["google.genai.types"] = genai_types_stub


@contextmanager
def _isolated_pipeline_dependencies():
    original_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if _is_pipeline_dependency_module(name)
    }
    missing_attribute = object()
    original_google = sys.modules.get("google")
    original_google_genai = (
        getattr(original_google, "genai", missing_attribute)
        if original_google is not None
        else missing_attribute
    )

    _stub_missing_pipeline_dependencies()
    try:
        # A fresh checkout intentionally has no ignored model files. Keep
        # collection network-free while main initializes the stub boundaries.
        with patch("urllib.request.urlretrieve", return_value=(None, None)):
            yield
    finally:
        # main keeps direct references to the test doubles it imported. Restore
        # the interpreter's module registry so later test modules see their real
        # dependencies (or skip them) instead of silently running against mocks.
        for module_name in tuple(sys.modules):
            if _is_pipeline_dependency_module(module_name) and module_name not in original_modules:
                sys.modules.pop(module_name, None)
        sys.modules.update(original_modules)
        if original_google is not None:
            if original_google_genai is missing_attribute:
                try:
                    delattr(original_google, "genai")
                except AttributeError:
                    pass
            else:
                original_google.genai = original_google_genai


with _isolated_pipeline_dependencies():
    import main


class _Reporter:
    def __init__(self):
        self.events = []
        self.stats_excluded_phases = set()

    def progress(self, *args, **kwargs):
        self.events.append(("progress", args, kwargs))

    def warning(self, *args, **kwargs):
        self.events.append(("warning", args, kwargs))

    def error(self, *args, **kwargs):
        self.events.append(("error", args, kwargs))

    def artifact(self, *args, **kwargs):
        self.events.append(("artifact", args, kwargs))

    def emit(self, *args, **kwargs):
        self.events.append(("emit", args, kwargs))

    def set_phase(self, *args, **kwargs):
        self.events.append(("phase", args, kwargs))

    def set_output_seconds(self, value):
        self.output_seconds = value

    @contextmanager
    def operation(self, *args, **kwargs):
        self.events.append(("operation", args, kwargs))
        yield


def _transcript(duration=900):
    return {
        "language": "de",
        "text": "Test transcript",
        "segments": [{
            "start": 0,
            "end": duration,
            "text": "Test transcript",
            "words": [],
        }],
    }


def _run_auto_with_both_outputs(
    monkeypatch, tmp_path, shorts_renderer, long_renderer, *, shorts=None,
):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setattr(main, "_render_shorts_clips", shorts_renderer)
    monkeypatch.setattr(main, "_render_longform_video", long_renderer)
    monkeypatch.setattr(
        main,
        "_analyze_longform_with_fallback",
        lambda *_args, **_kwargs: {
            "plan_data": {
                "viable": True,
                "video_title": "Long result",
                "youtube_description": "",
                "segments": [{
                    "start": 0,
                    "end": 500,
                    "chapter_title": "Story",
                    "role": "body",
                }],
                "total_duration": 500,
                "warnings": [],
            },
            "error": None,
            "attempts": [],
        },
    )
    metadata = main._run_video_type_pipeline(
        "auto",
        transcript=_transcript(),
        duration=900,
        analysis_result={
            "clips_data": {
                "shorts": shorts or [{
                    "start": 10,
                    "end": 40,
                    "video_title_for_youtube_short": "Short result",
                }],
            },
            "error": None,
            "attempts": [],
            "cost_analysis": None,
            "windows": [],
            "scored_windows": [],
        },
        output_dir=str(tmp_path),
        video_title="Video",
        input_video="source.mp4",
        output_format="vertical",
        layout_style="smart",
        resume_requested=False,
        resume_phase=None,
        metadata_file=str(tmp_path / "metadata.json"),
        analysis_result_file=str(tmp_path / "analysis.json"),
        source_url=None,
    )
    return metadata, reporter


@pytest.mark.parametrize("message", [
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "HTTP Error 429: Too Many Requests",
    "ERROR: fragment 3 not found",
    "Sign in to confirm you're not a bot",
    "ERROR: unable to download video data: <urlopen error [Errno 104] Connection reset by peer>",
    "ERROR: unable to download video data: Remote end closed connection without response",
])
def test_youtube_refusal_detection_matches_transfer_blocks(message):
    assert main._is_youtube_refusal(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "ERROR: [youtube] xY403abcDe: Video unavailable",
    "ERROR: Content too short (expected 12345 bytes and served 4291)",
    r"C:\videos\403\source.mp4: No space left on device",
    "Video titled Too Many Requests is unavailable",
    "Video unavailable",
])
def test_youtube_refusal_detection_ignores_unrelated_numbers(message):
    assert main._is_youtube_refusal(RuntimeError(message)) is False


def test_detail_failure_keeps_score_data_for_auto_longform(monkeypatch):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "key")

    scored = [{"id": "window_001", "start": 0, "end": 90, "score": 92, "reason": "strong"}]
    monkeypatch.setattr(
        main,
        "_run_score_stage",
        lambda *_args, **_kwargs: (scored, {"window_001"}, set(), [], []),
    )

    def fail_detail(*_args, **_kwargs):
        raise RuntimeError("detail unavailable")

    monkeypatch.setattr(main, "_call_gemini_worker", fail_detail)
    result = main.get_viral_clips(_transcript(90), 90)

    assert result["clips_data"] is None
    assert result["windows"][0]["id"] == "window_001"
    assert result["scored_windows"] == scored
    assert any(event[0] == "error" for event in reporter.events)


def test_auto_detail_failure_stays_nonterminal_when_longform_succeeds(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "key")

    scored = [{"id": "window_001", "start": 0, "end": 90, "score": 92, "reason": "strong"}]
    monkeypatch.setattr(
        main,
        "_run_score_stage",
        lambda *_args, **_kwargs: (scored, {"window_001"}, set(), [], []),
    )

    def fail_detail(*_args, **_kwargs):
        raise RuntimeError("detail unavailable")

    monkeypatch.setattr(
        main,
        "_call_gemini_worker",
        fail_detail,
    )
    monkeypatch.setattr(
        main,
        "_analyze_longform_with_fallback",
        lambda *_args, **_kwargs: {
            "plan_data": {
                "viable": True,
                "video_title": "Recovered long video",
                "youtube_description": "",
                "segments": [{
                    "start": 0,
                    "end": 500,
                    "chapter_title": "Story",
                    "role": "body",
                }],
                "total_duration": 500,
                "warnings": [],
            },
            "error": None,
            "attempts": [],
        },
    )
    rendered = []
    monkeypatch.setattr(
        main,
        "_render_longform_video",
        lambda *_args, **_kwargs: rendered.append(True) or str(tmp_path / "Video_long_1.mp4"),
    )

    metadata = main._run_video_type_pipeline(
        "auto",
        transcript=_transcript(),
        duration=900,
        analysis_result=None,
        output_dir=str(tmp_path),
        video_title="Video",
        input_video="source.mp4",
        output_format="vertical",
        layout_style="smart",
        resume_requested=False,
        resume_phase=None,
        metadata_file=str(tmp_path / "metadata.json"),
        analysis_result_file=str(tmp_path / "analysis.json"),
        source_url=None,
    )

    assert metadata["processing_mode"] == "long_video"
    assert metadata["analysis_status"] == "partial"
    assert rendered == [True]
    assert not any(event[0] == "error" for event in reporter.events)
    assert any(
        event[0] == "warning" and event[2].get("recoverable") is True
        for event in reporter.events
    )


def test_auto_continues_with_long_video_when_shorts_render_fails(monkeypatch, tmp_path):
    render_order = []

    def fail_shorts(*_args, **_kwargs):
        render_order.append("shorts")
        (tmp_path / "temp_Video_clip_1.mp4").write_bytes(b"temporary short")
        raise RuntimeError("shorts renderer unavailable")

    def render_long(*_args, **kwargs):
        render_order.append("long")
        assert kwargs["weight_done_before"] == 0.0
        assert kwargs["total_weight"] == pytest.approx(500.0)
        return str(tmp_path / "Video_long_1.mp4")

    metadata, reporter = _run_auto_with_both_outputs(
        monkeypatch, tmp_path, fail_shorts, render_long,
    )

    assert render_order == ["shorts", "long"]
    assert metadata["processing_mode"] == "long_video"
    assert metadata["analysis_status"] == "partial"
    assert metadata["shorts"] == []
    assert len(metadata["long_videos"]) == 1
    assert "shorts renderer unavailable" in metadata["render_errors"][0]
    assert reporter.output_seconds == pytest.approx(500.0)
    assert not (tmp_path / "temp_Video_clip_1.mp4").exists()
    assert json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8")) == metadata
    assert not any(event[0] == "error" for event in reporter.events)
    render_phases = [
        event for event in reporter.events
        if event[0] == "phase" and event[1][0] == "render"
    ]
    assert len(render_phases) == 2
    assert "independently" in render_phases[-1][2]["message"]


def test_auto_keeps_shorts_when_long_video_render_fails(monkeypatch, tmp_path):
    render_order = []

    def render_shorts(*_args, **_kwargs):
        render_order.append("shorts")
        return 30.0

    def fail_long(*_args, **_kwargs):
        render_order.append("long")
        (tmp_path / "temp_Video_long_seg_001.mp4").write_bytes(b"temporary segment")
        (tmp_path / "temp_Video_long_concat.txt").write_text("temporary manifest")
        (tmp_path / "temp_Video_long_joined.mp4").write_bytes(b"temporary joined video")
        raise RuntimeError("long renderer unavailable")

    metadata, reporter = _run_auto_with_both_outputs(
        monkeypatch, tmp_path, render_shorts, fail_long,
    )

    assert render_order == ["shorts", "long"]
    assert metadata["processing_mode"] == "clips"
    assert metadata["analysis_status"] == "partial"
    assert len(metadata["shorts"]) == 1
    assert metadata["long_videos"] == []
    assert "long renderer unavailable" in metadata["render_errors"][0]
    assert reporter.output_seconds == pytest.approx(30.0)
    assert not list(tmp_path.glob("temp_Video_long_*"))
    assert json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8")) == metadata
    assert not any(event[0] == "error" for event in reporter.events)


def test_auto_fails_only_after_both_planned_render_groups_fail(monkeypatch, tmp_path):
    render_order = []

    def fail_shorts(*_args, **_kwargs):
        render_order.append("shorts")
        raise RuntimeError("shorts renderer unavailable")

    def fail_long(*_args, **_kwargs):
        render_order.append("long")
        raise RuntimeError("long renderer unavailable")

    with pytest.raises(RuntimeError, match="All planned Auto outputs failed to render"):
        _run_auto_with_both_outputs(monkeypatch, tmp_path, fail_shorts, fail_long)

    assert render_order == ["shorts", "long"]


def test_auto_preserves_completed_shorts_when_later_renders_fail(monkeypatch, tmp_path):
    render_order = []

    def partially_render_shorts(clips_data, *_args, **kwargs):
        render_order.append("shorts")
        kwargs["completed_callback"](0, clips_data["shorts"][0])
        raise RuntimeError("second short failed")

    def fail_long(*_args, **_kwargs):
        render_order.append("long")
        raise RuntimeError("long renderer unavailable")

    metadata, reporter = _run_auto_with_both_outputs(
        monkeypatch,
        tmp_path,
        partially_render_shorts,
        fail_long,
        shorts=[
            {
                "start": 10,
                "end": 40,
                "video_title_for_youtube_short": "Completed short",
            },
            {
                "start": 50,
                "end": 80,
                "video_title_for_youtube_short": "Failed short",
            },
        ],
    )

    assert render_order == ["shorts", "long"]
    assert metadata["processing_mode"] == "clips"
    assert metadata["analysis_status"] == "partial"
    assert [item["video_title_for_youtube_short"] for item in metadata["shorts"]] == [
        "Completed short",
    ]
    assert metadata["long_videos"] == []
    assert len(metadata["render_errors"]) == 2
    assert reporter.output_seconds == pytest.approx(30.0)
    assert any(
        event[0] == "warning"
        and event[2].get("category") == "render"
        and event[2].get("recoverable") is True
        for event in reporter.events
    )


def test_auto_invalid_short_payload_reaches_bounded_fallback(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    scored = [{"id": "window_001", "start": 0, "end": 120, "score": 92, "reason": "strong"}]
    monkeypatch.setattr(
        main,
        "_run_score_stage",
        lambda *_args, **_kwargs: (scored, {"window_001"}, set(), [], []),
    )
    monkeypatch.setattr(
        main,
        "_call_gemini_worker",
        lambda *_args, **_kwargs: {
            "payload": {"shorts": [{"start": "invalid", "end": 30}]},
            "cost_analysis": None,
        },
    )
    monkeypatch.setattr(
        main,
        "_analyze_longform_with_fallback",
        lambda *_args, **_kwargs: pytest.fail("short Auto sources must skip long-form planning"),
    )

    def render_fallback(_input, output, **_kwargs):
        Path(output).write_bytes(b"video")
        return True

    monkeypatch.setattr(main, "_render_clip", render_fallback)

    metadata = main._run_video_type_pipeline(
        "auto",
        transcript=_transcript(120),
        duration=120,
        analysis_result=None,
        output_dir=str(tmp_path),
        video_title="Video",
        input_video="source.mp4",
        output_format="vertical",
        layout_style="smart",
        resume_requested=False,
        resume_phase=None,
        metadata_file=str(tmp_path / "metadata.json"),
        analysis_result_file=str(tmp_path / "analysis.json"),
        source_url=None,
    )

    assert metadata["processing_mode"] == "full_video_fallback"
    assert "failed validation" in metadata["analysis_error"]
    assert not any(event[0] == "error" for event in reporter.events)
    assert any(
        event[0] == "warning" and event[2].get("recoverable") is True
        for event in reporter.events
    )


def test_auto_uses_full_video_fallback_when_no_edit_is_viable(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)

    def no_shorts(*_args, **kwargs):
        assert kwargs["defer_terminal_error"] is True
        return {
            "clips_data": None,
            "error": "no valid Shorts",
            "attempts": [{"stage": "detail", "status": "failed"}],
            "cost_analysis": None,
            "windows": [],
            "scored_windows": [],
        }

    monkeypatch.setattr(main, "get_viral_clips", no_shorts)
    monkeypatch.setattr(
        main,
        "_analyze_longform_with_fallback",
        lambda *_args, **_kwargs: pytest.fail("short Auto sources must skip long-form planning"),
    )
    render_calls = []

    def render_fallback(_input, output, **kwargs):
        render_calls.append(kwargs)
        Path(output).write_bytes(b"video")
        return True

    monkeypatch.setattr(main, "_render_clip", render_fallback)
    metadata_file = tmp_path / "metadata.json"

    metadata = main._run_video_type_pipeline(
        "auto",
        transcript=_transcript(120),
        duration=120,
        analysis_result=None,
        output_dir=str(tmp_path),
        video_title="Video",
        input_video="source.mp4",
        output_format="vertical",
        layout_style="smart",
        resume_requested=False,
        resume_phase=None,
        metadata_file=str(metadata_file),
        analysis_result_file=str(tmp_path / "analysis.json"),
        source_url=None,
    )

    assert metadata["processing_mode"] == "full_video_fallback"
    assert metadata["video_type"] == "auto"
    assert metadata["long_video_skipped_reason"].startswith("source_too_short")
    assert len(render_calls) == 1
    assert render_calls[0]["output_format"] == "vertical"
    assert render_calls[0]["layout_style"] == "smart"
    assert callable(render_calls[0]["progress_callback"])
    assert json.loads(metadata_file.read_text(encoding="utf-8"))["video_type"] == "auto"
    assert not any(event[0] == "error" for event in reporter.events)


def test_auto_does_not_render_unbounded_full_video_fallback(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setattr(main, "AUTO_FULL_VIDEO_FALLBACK_MAX_SECONDS", 600)
    monkeypatch.setattr(
        main,
        "get_viral_clips",
        lambda *_args, **_kwargs: {
            "clips_data": None,
            "error": "no valid Shorts",
            "attempts": [],
            "cost_analysis": None,
            "windows": [],
            "scored_windows": [],
        },
    )
    monkeypatch.setattr(
        main,
        "_analyze_longform_with_fallback",
        lambda *_args, **_kwargs: {
            "plan_data": None,
            "error": "no coherent long plan",
            "attempts": [],
        },
    )
    monkeypatch.setattr(
        main,
        "_render_clip",
        lambda *_args, **_kwargs: pytest.fail("oversized Auto fallback must not render"),
    )

    with pytest.raises(RuntimeError, match=r"7200s exceeds the configured 600s limit"):
        main._run_video_type_pipeline(
            "auto",
            transcript=_transcript(7200),
            duration=7200,
            analysis_result=None,
            output_dir=str(tmp_path),
            video_title="LongSource",
            input_video="input.mp4",
            output_format="vertical",
            layout_style="smart",
            resume_requested=False,
            resume_phase=None,
            metadata_file=str(tmp_path / "metadata.json"),
            analysis_result_file=str(tmp_path / "analysis.json"),
            source_url=None,
        )


def test_auto_does_not_render_full_video_when_duration_is_unknown(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setattr(
        main,
        "_render_clip",
        lambda *_args, **_kwargs: pytest.fail("unknown-duration Auto fallback must not render"),
    )

    with pytest.raises(RuntimeError, match="source duration is unknown"):
        main._run_video_type_pipeline(
            "auto",
            transcript=_transcript(0),
            duration=0,
            analysis_result={
                "clips_data": None,
                "error": "no valid Shorts",
                "attempts": [],
                "cost_analysis": None,
                "windows": [],
                "scored_windows": [],
            },
            output_dir=str(tmp_path),
            video_title="UnknownDuration",
            input_video="input.mp4",
            output_format="vertical",
            layout_style="smart",
            resume_requested=False,
            resume_phase=None,
            metadata_file=str(tmp_path / "metadata.json"),
            analysis_result_file=str(tmp_path / "analysis.json"),
            source_url=None,
        )


def test_long_mode_ignores_stale_shorts_resume_analysis(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    long_result = {
        "plan_data": {
            "viable": True,
            "video_title": "Long result",
            "youtube_description": "",
            "segments": [{
                "start": 0,
                "end": 500,
                "chapter_title": "Story",
                "role": "body",
            }],
            "total_duration": 500,
            "warnings": [],
        },
        "error": None,
        "attempts": [{"stage": "longform_plan_v2", "status": "success"}],
        "cost_analysis": {"total_cost": 0.1},
    }
    monkeypatch.setattr(main, "_analyze_longform_with_fallback", lambda *_args, **_kwargs: long_result)
    monkeypatch.setattr(main, "_render_longform_video", lambda *_args, **_kwargs: "long.mp4")

    metadata = main._run_video_type_pipeline(
        "long",
        transcript=_transcript(),
        duration=900,
        analysis_result={
            "error": "stale Shorts failure",
            "attempts": [{"stage": "detail", "status": "failed"}],
            "cost_analysis": {"total_cost": 99},
        },
        output_dir=str(tmp_path),
        video_title="Video",
        input_video="source.mp4",
        output_format="vertical",
        layout_style="smart",
        resume_requested=True,
        resume_phase="render",
        metadata_file=str(tmp_path / "metadata.json"),
        analysis_result_file=str(tmp_path / "analysis.json"),
        source_url=None,
    )

    assert metadata["analysis_status"] == "success"
    assert metadata["analysis_error"] is None
    assert metadata["analysis_attempts"] == long_result["attempts"]
    assert "stale Shorts failure" not in json.dumps(metadata)


def test_score_stage_recovers_every_window_omitted_by_a_batch(monkeypatch):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    windows = [
        {"id": f"window_{index:03d}", "start": index * 60, "end": (index + 1) * 60, "text": "text"}
        for index in range(9)
    ]

    def fake_worker(_mode, payload, **_kwargs):
        batch = payload["windows"]
        returned = batch if len(batch) == 1 else batch[:3]
        return {
            "payload": {
                "windows": [{
                    "id": item["id"],
                    "start": item["start"] + 5,
                    "end": item["end"] - 5,
                    "score": 50,
                    "reason": "scored",
                } for item in returned],
            },
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)

    scores, processed, skipped, attempts, _costs = main._run_score_stage(
        windows, "en", 540, None, None,
    )

    assert [item["id"] for item in scores] == [item["id"] for item in windows]
    assert [(item["start"], item["end"]) for item in scores] == [
        (item["start"], item["end"]) for item in windows
    ]
    assert processed == {item["id"] for item in windows}
    assert skipped == set()
    assert sum(item["name"] == "single-window-rescue" for item in attempts) == 5


def test_score_stage_caps_individual_rescue_calls_per_job(monkeypatch):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setattr(main, "GEMINI_MAX_SCORE_RESCUE_CALLS", 2)
    windows = [
        {"id": f"window_{index:03d}", "start": index * 60, "end": (index + 1) * 60, "text": "text"}
        for index in range(8)
    ]
    calls = []

    def fake_worker(_mode, payload, **_kwargs):
        batch = payload["windows"]
        calls.append([item["id"] for item in batch])
        returned = batch[:1]
        return {
            "payload": {
                "windows": [{
                    "id": item["id"],
                    "start": item["start"],
                    "end": item["end"],
                    "score": 50,
                    "reason": "scored",
                } for item in returned],
            },
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)

    _scores, processed, skipped, _attempts, _costs = main._run_score_stage(
        windows, "en", 480, None, None,
    )

    assert len(calls) == 3  # one batch plus the configured two individual rescues
    assert len(processed) == 3
    assert len(skipped) == 5
    assert any(
        event[0] == "warning" and "per-job limit" in event[1][0]
        for event in reporter.events
    )


@pytest.mark.parametrize("resume_phase", [None, "render"])
def test_longform_resume_reuses_valid_checkpoint_without_gemini(monkeypatch, tmp_path, resume_phase):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    transcript = _transcript()
    fingerprint = main.longform.transcript_fingerprint(
        main.longform.build_editorial_units(transcript, 900),
    )
    checkpoint = {
        "plan_data": {
            "viable": True,
            "planner_version": 2,
            "quality_gate_version": main.longform.QUALITY_GATE_VERSION,
            "transcript_fingerprint": fingerprint,
            "editorial_review": {"approved": True},
            "segments": [{"start": 0, "end": 500, "role": "body"}],
            "total_duration": 500,
        }
    }
    path = tmp_path / "Video_longform_result.json"
    path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(
        main, "get_longform_plan",
        lambda *_args, **_kwargs: pytest.fail("resume must not spend another Gemini request"),
    )

    result = main._analyze_longform_with_fallback(
        transcript, 900, output_dir=str(tmp_path), video_title="Video",
        resume_requested=True, resume_phase=resume_phase,
    )
    assert result == checkpoint


def test_fresh_longform_run_ignores_checkpoint_from_reused_output_directory(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    stale = {
        "plan_data": {
            "viable": True,
            "segments": [{"start": 0, "end": 500, "role": "body"}],
            "total_duration": 500,
        }
    }
    fresh = {
        "plan_data": {
            "viable": True,
            "segments": [{"start": 120, "end": 620, "role": "body"}],
            "total_duration": 500,
        }
    }
    path = tmp_path / "Video_longform_result.json"
    path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(main, "get_longform_plan", lambda *_args, **_kwargs: fresh)

    result = main._analyze_longform_with_fallback(
        _transcript(), 900, output_dir=str(tmp_path), video_title="Video",
        resume_requested=False, resume_phase=None,
    )

    assert result == fresh
    assert json.loads(path.read_text(encoding="utf-8")) == fresh


def test_long_mode_never_uses_full_source_passthrough_when_plan_is_impossible(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setattr(
        main, "_analyze_longform_with_fallback",
        lambda *_args, **_kwargs: {"plan_data": None, "error": "no coherent plan", "attempts": []},
    )
    monkeypatch.setattr(
        main, "_render_clip",
        lambda *_args, **_kwargs: pytest.fail("Long mode must not pass through the full source"),
    )

    with pytest.raises(RuntimeError, match="No viable Shorts or bounded long-form"):
        main._run_video_type_pipeline(
            "long",
            transcript=_transcript(),
            duration=900,
            analysis_result=None,
            output_dir=str(tmp_path),
            video_title="Video",
            input_video="source.mp4",
            output_format="vertical",
            layout_style="smart",
            resume_requested=False,
            resume_phase=None,
            metadata_file=str(tmp_path / "metadata.json"),
            analysis_result_file=str(tmp_path / "analysis.json"),
            source_url=None,
        )


def test_non_16_9_long_render_uses_canvas_and_duration_cuts(monkeypatch, tmp_path):
    reporter = _Reporter()
    commands = []
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setattr(main, "get_video_resolution", lambda _path: (640, 480))
    monkeypatch.setattr(main, "_run_checked_ffmpeg", lambda command, **_kwargs: commands.append(command))

    def finalize(_input, output, _progress):
        with open(output, "wb") as handle:
            handle.write(b"video")
        return True

    monkeypatch.setattr(main, "_finalize_clip_passthrough", finalize)
    plan = {
        "total_duration": 60,
        "segments": [
            {"start": 10, "end": 40},
            {"start": 80, "end": 110},
        ],
    }

    output = main._render_longform_video(
        plan, "source.mp4", str(tmp_path), "Video", total_weight=60,
    )

    assert os.path.basename(output) == "Video_long_1.mp4"
    assert "-filter_complex" in commands[0]
    assert commands[0][commands[0].index("-t") + 1] == "30.000"
    assert "-to" not in commands[0]
    assert commands[-1][commands[-1].index("-c:v") + 1] == "copy"


def test_source_discovery_excludes_finished_long_output(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    long_output = tmp_path / "source_long_1.mp4"
    source.write_bytes(b"source")
    long_output.write_bytes(b"much larger generated long output")
    monkeypatch.setattr(main, "_probe_stream_types", lambda _path: {"video", "audio"})

    assert main._find_source_video(str(tmp_path)) == str(source)


def test_download_resume_cleanup_never_deletes_finished_long_output(monkeypatch, tmp_path):
    long_output = tmp_path / "source_long_1.mp4"
    long_output.write_bytes(b"video")
    monkeypatch.setattr(main, "_probe_stream_types", lambda _path: set())

    removed = main._clean_partial_download(str(tmp_path))

    assert removed == []
    assert long_output.exists()


def test_auto_skips_nine_minute_sources_but_long_mode_still_scales():
    """Auto needs headroom: a best-of from nine minutes is nearly the full clip.

    Explicit Long mode keeps the lower physical bound and adapts its target.
    """
    assert main.LONGFORM_MIN_SOURCE_SECONDS > 9 * 60
    assert main.LONGFORM_HARD_MIN_SOURCE_SECONDS <= 9 * 60
    target_min, target_max, _warnings = main._longform_target_range(9 * 60)
    assert target_min == 240
    assert target_max == 540


def test_video_duration_falls_back_to_ffprobe_when_opencv_is_unknown(monkeypatch):
    class UnknownDurationCapture:
        released = False

        def get(self, _property):
            return 0

        def release(self):
            self.released = True

    capture = UnknownDurationCapture()
    commands = []
    monkeypatch.setattr(main.cv2, "VideoCapture", lambda _path: capture)

    def probe(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"725.500000\n")

    monkeypatch.setattr(main.subprocess, "run", probe)

    duration = main._get_video_duration("source.mp4")

    assert duration == pytest.approx(725.5)
    assert capture.released is True
    assert commands[0][0][0] == "ffprobe"
    assert commands[0][1]["timeout"] == 60
    main._validate_longform_source_duration("long", duration)


def test_unknown_long_duration_has_distinct_error_when_all_probes_fail(monkeypatch):
    capture = SimpleNamespace(
        get=lambda _property: 0,
        release=lambda: None,
    )
    monkeypatch.setattr(main.cv2, "VideoCapture", lambda _path: capture)

    def missing_probe(*_args, **_kwargs):
        raise FileNotFoundError("ffprobe missing")

    monkeypatch.setattr(main.subprocess, "run", missing_probe)

    duration = main._get_video_duration("source.mp4")

    assert duration == 0.0
    with pytest.raises(RuntimeError, match="could not determine the source duration"):
        main._validate_longform_source_duration("long", duration)


def test_explicit_long_source_is_rejected_before_transcription():
    with pytest.raises(RuntimeError, match="needs at least"):
        main._validate_longform_source_duration(
            "long", main.LONGFORM_HARD_MIN_SOURCE_SECONDS - 1,
        )

    main._validate_longform_source_duration("auto", 1)
    source = Path(main.__file__).read_text(encoding="utf-8")
    cli_start = source.index("if __name__ == '__main__':")
    early_guard = source.index(
        "_validate_longform_source_duration(video_type, duration)", cli_start,
    )
    transcription = source.index("transcript = transcribe_video(input_video, duration)", cli_start)
    assert early_guard < transcription


def _v2_word_transcript():
    segments = []
    for index in range(6):
        start = index * 60.0
        end = (index + 1) * 60.0
        segments.append({
            "start": start,
            "end": end,
            "text": f"Vollständiger Gedanke {index + 1}.",
            "words": [{
                "word": f" Vollständiger Gedanke {index + 1}.",
                "start": start,
                "end": end,
            }],
        })
    return {
        "language": "de",
        "text": " ".join(item["text"] for item in segments),
        "segments": segments,
    }


def _v2_raw_plan():
    return {
        "viable": True,
        "video_title": "Geprüfter Schnitt",
        "youtube_description": "Beschreibung",
        "recommended_duration_seconds": 300,
        "duration_reason": "Fünf starke Einheiten.",
        "cold_open": None,
        "chapters": [
            {
                "id": "chapter_01",
                "title": "Erstes Thema",
                "topic": "Erstes Thema",
                "priority": 90,
                "reason": "Wichtig",
                "spans": [{
                    "id": "span_01",
                    "start_unit_id": "u000001",
                    "end_unit_id": "u000003",
                }],
            },
            {
                "id": "chapter_02",
                "title": "Zweites Thema",
                "topic": "Zweites Thema",
                "priority": 80,
                "reason": "Ebenfalls stark",
                "spans": [{
                    "id": "span_02",
                    "start_unit_id": "u000004",
                    "end_unit_id": "u000005",
                }],
            },
        ],
    }


def _v2_review(plan, *, approved, score, dropped_chapter_ids=None):
    segment_ids = []
    if isinstance(plan.get("cold_open"), dict):
        segment_ids.append(str(plan["cold_open"].get("id") or "cold_open"))
    segment_ids.extend(
        str(span.get("id") or "")
        for chapter in plan.get("chapters") or []
        if isinstance(chapter, dict)
        for span in chapter.get("spans") or []
        if isinstance(span, dict)
    )
    return {
        "approved": approved,
        "overall_score": score,
        "ending_complete": True,
        "critical_issues": [],
        "dropped_chapter_ids": list(dropped_chapter_ids or []),
        "boundary_reviews": [{
            "segment_id": segment_id,
            "opening_complete": approved,
            "ending_complete": approved,
            "continuation_needed": False,
            "issue": "" if approved else "Unvollständige Grenze",
        } for segment_id in segment_ids],
        "joins": [{
            "id": f"{left}->{right}",
            "score": score,
            "context_complete": approved,
            "issue": "" if approved else "Abrupter Übergang",
        } for left, right in zip(segment_ids, segment_ids[1:])],
        "plan": plan,
    }


def test_longform_quality_gate_runs_one_repair_before_accepting(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    calls = []

    def fake_worker(mode, payload, **_kwargs):
        calls.append((
            mode,
            bool(payload.get("repair_required")),
            bool(payload.get("final_verification")),
        ))
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        if payload.get("final_verification"):
            return {"payload": _v2_review(raw_plan, approved=True, score=93), "cost_analysis": None}
        if payload.get("repair_required"):
            return {"payload": _v2_review(raw_plan, approved=True, score=92), "cost_analysis": None}
        return {"payload": _v2_review(raw_plan, approved=False, score=70), "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert result["plan_data"]["viable"] is True
    assert result["plan_data"]["editorial_review"]["repair_attempted"] is True
    assert calls == [
        ("longform_plan_v2", False, False),
        ("longform_review", False, False),
        ("longform_review", True, False),
        ("longform_review", False, True),
    ]
    assert result["plan_data"]["editorial_review"]["final_verification_attempted"] is True


def test_repair_rolls_back_both_plan_views_after_empty_review_plan(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    empty_plan = copy.deepcopy(raw_plan)
    empty_plan["viable"] = False
    empty_plan["chapters"] = []
    repair_inputs = []

    def fake_worker(mode, payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        if payload.get("final_verification"):
            return {
                "payload": _v2_review(raw_plan, approved=True, score=94),
                "cost_analysis": None,
            }
        if payload.get("repair_required"):
            repair_inputs.append({
                "viable": payload["draft_plan"]["viable"],
                "chapters": len(payload["draft_plan"]["chapters"]),
                "context_segments": len(payload["review_context"]["assembled_segments"]),
            })
            return {
                "payload": _v2_review(raw_plan, approved=True, score=93),
                "cost_analysis": None,
            }
        return {
            "payload": _v2_review(empty_plan, approved=False, score=60),
            "cost_analysis": None,
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert repair_inputs == [{"viable": True, "chapters": 2, "context_segments": 2}]


def test_repair_context_uses_current_boundaries_with_anchor_candidates(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    moved_plan = copy.deepcopy(raw_plan)
    moved_plan["chapters"][0]["spans"][0]["end_unit_id"] = "u000002"
    first_end_candidates = []
    repair_observation = {}

    def neighborhood_for(payload, segment_id):
        return next(
            item
            for item in payload["review_context"]["boundary_neighborhoods"]
            if item["segment_id"] == segment_id
        )

    def fake_worker(mode, payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        if payload.get("final_verification"):
            return {
                "payload": _v2_review(moved_plan, approved=True, score=94),
                "cost_analysis": None,
            }
        span_neighborhood = neighborhood_for(payload, "span_01")
        if payload.get("repair_required"):
            repair_observation.update({
                "draft_end": payload["draft_plan"]["chapters"][0]["spans"][0]["end_unit_id"],
                "context_end": span_neighborhood["current_end_unit_id"],
                "end_candidates": [
                    item["id"] for item in span_neighborhood["end_candidate_units"]
                ],
            })
            return {
                "payload": _v2_review(moved_plan, approved=True, score=93),
                "cost_analysis": None,
            }
        first_end_candidates.extend(
            item["id"] for item in span_neighborhood["end_candidate_units"]
        )
        return {
            "payload": _v2_review(moved_plan, approved=False, score=70),
            "cost_analysis": None,
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert repair_observation == {
        "draft_end": "u000002",
        "context_end": "u000002",
        "end_candidates": first_end_candidates,
    }


def test_out_of_order_model_chapters_are_normalized_before_review(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    raw_plan["chapters"] = list(reversed(raw_plan["chapters"]))
    reviewed_order = []

    def fake_worker(mode, payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        reviewed_order.extend(chapter["id"] for chapter in payload["draft_plan"]["chapters"])
        return {"payload": _v2_review(payload["draft_plan"], approved=True, score=92), "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert reviewed_order == ["chapter_01", "chapter_02"]
    assert result["plan_data"]["chronology_normalized"] is True
    assert result["plan_data"]["dropped_topics"] == []
    assert [
        segment["chapter_id"]
        for segment in result["plan_data"]["segments"]
        if segment["role"] != "cold_open"
    ] == ["chapter_01", "chapter_02"]


def test_fresh_final_review_can_reject_a_self_approved_repair(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()

    def fake_worker(mode, payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        if payload.get("final_verification"):
            final_review = _v2_review(raw_plan, approved=False, score=72)
            final_review["ending_complete"] = False
            final_review["boundary_reviews"][-1]["ending_complete"] = False
            final_review["boundary_reviews"][-1]["continuation_needed"] = True
            return {"payload": final_review, "cost_analysis": None}
        if payload.get("repair_required"):
            return {"payload": _v2_review(raw_plan, approved=True, score=92), "cost_analysis": None}
        return {"payload": _v2_review(raw_plan, approved=False, score=70), "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is not None
    assert result["plan_data"]["viable"] is False
    assert result["plan_data"]["editorial_review"]["final_verification_attempted"] is True
    assert "incomplete_ending" in result["plan_data"]["editorial_review"]["gate_issues"]
    assert "incomplete_segment_ending:span_02" in result["plan_data"]["editorial_review"]["gate_issues"]


def test_repair_cannot_delete_a_strong_chapter_that_sorting_preserved(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    raw_plan["chapters"].append({
        "id": "chapter_03",
        "title": "Drittes Thema",
        "topic": "Drittes Thema",
        "priority": 75,
        "reason": "Ebenfalls stark",
        "spans": [{
            "id": "span_03",
            "start_unit_id": "u000006",
            "end_unit_id": "u000006",
        }],
    })
    raw_plan["chapters"] = [
        raw_plan["chapters"][1],
        raw_plan["chapters"][0],
        raw_plan["chapters"][2],
    ]
    repair_input_chapters = []

    def fake_worker(mode, payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        if payload.get("final_verification"):
            return {"payload": _v2_review(payload["draft_plan"], approved=True, score=94), "cost_analysis": None}
        if payload.get("repair_required"):
            repair_input_chapters.extend(chapter["id"] for chapter in payload["draft_plan"]["chapters"])
            return {"payload": _v2_review(payload["draft_plan"], approved=True, score=93), "cost_analysis": None}
        dropped = copy.deepcopy(payload["draft_plan"])
        dropped["chapters"] = [
            chapter for chapter in dropped["chapters"] if chapter["id"] != "chapter_02"
        ]
        return {
            "payload": _v2_review(
                dropped,
                approved=True,
                score=95,
                dropped_chapter_ids=["chapter_02"],
            ),
            "cost_analysis": None,
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert repair_input_chapters == ["chapter_01", "chapter_02", "chapter_03"]
    assert result["plan_data"]["dropped_topics"] == []
    assert {
        segment["chapter_id"]
        for segment in result["plan_data"]["segments"]
        if segment["role"] != "cold_open"
    } == {"chapter_01", "chapter_02", "chapter_03"}


def test_review_can_drop_an_unmoved_chapter_after_chronology_normalization(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    raw_plan["chapters"].append({
        "id": "chapter_03",
        "title": "Drittes Thema",
        "topic": "Drittes Thema",
        "priority": 60,
        "reason": "Optional",
        "spans": [{
            "id": "span_03",
            "start_unit_id": "u000006",
            "end_unit_id": "u000006",
        }],
    })
    raw_plan["chapters"] = [
        raw_plan["chapters"][1],
        raw_plan["chapters"][0],
        raw_plan["chapters"][2],
    ]

    def fake_worker(mode, payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        reviewed = copy.deepcopy(payload["draft_plan"])
        reviewed["chapters"] = [
            chapter for chapter in reviewed["chapters"] if chapter["id"] != "chapter_03"
        ]
        return {
            "payload": _v2_review(
                reviewed,
                approved=True,
                score=95,
                dropped_chapter_ids=["chapter_03"],
            ),
            "cost_analysis": None,
        }

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert result["plan_data"]["chronology_normalized"] is True
    assert result["plan_data"]["dropped_topics"] == ["chapter_03"]
    assert {
        segment["chapter_id"]
        for segment in result["plan_data"]["segments"]
        if segment["role"] != "cold_open"
    } == {"chapter_01", "chapter_02"}


@pytest.mark.parametrize("bad_payload", [[], "text", 42])
def test_longform_planner_non_object_payload_fails_cleanly(monkeypatch, tmp_path, bad_payload):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(
        main,
        "_call_gemini_worker",
        lambda *_args, **_kwargs: {"payload": bad_payload, "cost_analysis": None},
    )

    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["plan_data"] is None
    assert "non-object JSON payload" in result["error"]
    assert result["attempts"]
    assert all(attempt["status"] == "failed" for attempt in result["attempts"])


@pytest.mark.parametrize("malformed_cost_analysis", ["bad", {"input_tokens": "bad"}])
def test_longform_ignores_malformed_worker_cost_analysis(
    monkeypatch,
    tmp_path,
    malformed_cost_analysis,
):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()

    def fake_worker(mode, payload, **_kwargs):
        worker_payload = (
            raw_plan
            if mode == "longform_plan_v2"
            else _v2_review(payload["draft_plan"], approved=True, score=92)
        )
        return {"payload": worker_payload, "cost_analysis": malformed_cost_analysis}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is None
    assert result["cost_analysis"] is None


def test_malformed_nested_review_payload_rejects_cleanly(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()

    def fake_worker(mode, _payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        malformed_review = _v2_review(raw_plan, approved=True, score=95)
        malformed_review["joins"] = 42
        return {"payload": malformed_review, "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is not None
    assert result["plan_data"]["viable"] is False
    assert "invalid_review_payload:joins" in (
        result["plan_data"]["editorial_review"]["gate_issues"]
    )


def _v1_style_single_chapter_plan():
    """A draft that put both spans into one chapter — a one-topic compilation."""
    plan = _v2_raw_plan()
    merged = dict(plan["chapters"][0])
    merged["spans"] = [
        plan["chapters"][0]["spans"][0],
        plan["chapters"][1]["spans"][0],
    ]
    plan["chapters"] = [merged]
    return plan


def test_single_chapter_draft_triggers_repair_and_passes_once_regrouped(monkeypatch, tmp_path):
    """The chapter minimum must be repairable, not an instant rejection.

    The review keeps every span id and its unit boundaries and only splits the
    spans across two chapters — exactly what the repair prompt asks for.
    """
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    single = _v1_style_single_chapter_plan()
    regrouped = _v2_raw_plan()
    calls = []

    def fake_worker(mode, payload, **_kwargs):
        calls.append((
            mode,
            bool(payload.get("repair_required")),
            bool(payload.get("final_verification")),
        ))
        if mode == "longform_plan_v2":
            return {"payload": single, "cost_analysis": None}
        if payload.get("final_verification"):
            return {"payload": _v2_review(regrouped, approved=True, score=93), "cost_analysis": None}
        if payload.get("repair_required"):
            return {"payload": _v2_review(regrouped, approved=True, score=92), "cost_analysis": None}
        return {"payload": _v2_review(single, approved=True, score=95), "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    # A flawless self-review on a single-chapter plan must not pass the gate.
    assert calls == [
        ("longform_plan_v2", False, False),
        ("longform_review", False, False),
        ("longform_review", True, False),
        ("longform_review", False, True),
    ]
    assert result["error"] is None
    assert result["plan_data"]["viable"] is True
    assert result["plan_data"]["editorial_review"]["repair_attempted"] is True
    chapters = {segment["chapter_id"] for segment in result["plan_data"]["segments"]}
    assert chapters == {"chapter_01", "chapter_02"}


def test_single_chapter_draft_is_rejected_when_the_repair_keeps_one_topic(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    single = _v1_style_single_chapter_plan()

    def fake_worker(mode, _payload, **_kwargs):
        if mode == "longform_plan_v2":
            return {"payload": single, "cost_analysis": None}
        return {"payload": _v2_review(single, approved=True, score=98), "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert result["error"] is not None
    assert result["plan_data"]["viable"] is False
    assert "too_few_chapters" in result["plan_data"]["editorial_review"]["gate_issues"]


def test_longform_rejects_only_after_failed_repair_retry(monkeypatch, tmp_path):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    raw_plan = _v2_raw_plan()
    review_calls = 0

    def fake_worker(mode, _payload, **_kwargs):
        nonlocal review_calls
        if mode == "longform_plan_v2":
            return {"payload": raw_plan, "cost_analysis": None}
        review_calls += 1
        return {"payload": _v2_review(raw_plan, approved=False, score=70), "cost_analysis": None}

    monkeypatch.setattr(main, "_call_gemini_worker", fake_worker)
    result = main.get_longform_plan(
        _v2_word_transcript(),
        360,
        output_dir=str(tmp_path),
        video_title="Video",
        windows=[{"id": "window_001", "start": 0, "end": 360, "text": "text"}],
        scored_windows=[{"id": "window_001", "start": 0, "end": 360, "score": 90, "reason": "strong"}],
    )

    assert review_calls == 2
    assert result["plan_data"]["viable"] is False
    assert "after repair" in result["error"]


def test_local_scene_detection_never_decodes_more_than_its_bounded_window(monkeypatch):
    class Frame:
        size = 1
        shape = (10, 10)

    class Capture:
        def __init__(self):
            self.reads = 0
            self.start_ms = 0.0
            self.released = False

        def isOpened(self):
            return True

        def set(self, prop, value):
            if prop == main.cv2.CAP_PROP_POS_MSEC:
                self.start_ms = value

        def get(self, prop):
            if prop == main.cv2.CAP_PROP_FPS:
                return 30.0
            if prop == main.cv2.CAP_PROP_POS_MSEC:
                return self.start_ms + (self.reads * 1000.0 / 30.0)
            return 0.0

        def read(self):
            self.reads += 1
            return True, Frame()

        def release(self):
            self.released = True

    capture = Capture()
    monkeypatch.setattr(main.cv2, "VideoCapture", lambda _path: capture)
    monkeypatch.setattr(main.cv2, "cvtColor", lambda frame, _mode: frame)
    monkeypatch.setattr(main.cv2, "absdiff", lambda _left, _right: 0)
    # CI stubs numpy without "mean"; raising=False lets the patch apply there too.
    monkeypatch.setattr(main.np, "mean", lambda _value: 0.0, raising=False)
    monkeypatch.setattr(main, "LONGFORM_SCENE_SCAN_MAX_SECONDS", 1.5)
    monkeypatch.setattr(main, "LONGFORM_SCENE_CUT_THRESHOLD", 0.0)

    cut = main._find_local_scene_cut("source.mp4", 0.0, 100.0, 50.0)

    assert cut is not None
    assert capture.reads <= 49
    assert capture.start_ms >= 49_000
    assert capture.released is True


def test_scene_alignment_reverts_when_it_would_break_adaptive_duration(monkeypatch):
    plan = {
        "planner_version": 2,
        "target_min_seconds": 40.0,
        "target_max_seconds": 40.0,
        "total_duration": 40.0,
        "segments": [{
            "segment_id": "span_01",
            "start": 10.0,
            "end": 50.0,
            "start_cut_window": [9.0, 11.0],
            "end_cut_window": [49.0, 51.0],
        }],
    }
    scene_cuts = iter([9.0, 51.0])
    monkeypatch.setattr(main, "_find_local_scene_cut", lambda *_args: next(scene_cuts))

    refined = main._refine_longform_cut_plan(plan, "source.mp4")

    assert refined["total_duration"] == 40.0
    assert refined["segments"][0]["start"] == 10.0
    assert refined["segments"][0]["end"] == 50.0
    assert refined["local_scene_alignment"]["reverted_for_duration"] is True
    assert refined["local_scene_alignment"]["attempted_aligned_edges"] == 2


def test_scene_alignment_reverts_when_a_segment_would_become_too_short(monkeypatch):
    plan = {
        "planner_version": 2,
        "target_min_seconds": 0.0,
        "target_max_seconds": 100.0,
        "total_duration": 20.0,
        "segments": [{
            "segment_id": "span_01",
            "role": "setup",
            "start": 10.0,
            "end": 30.0,
            "start_cut_window": [10.0, 12.0],
            "end_cut_window": [28.0, 30.0],
        }],
    }
    scene_cuts = iter([12.0, 28.0])
    monkeypatch.setattr(main, "_find_local_scene_cut", lambda *_args: next(scene_cuts))

    refined = main._refine_longform_cut_plan(plan, "source.mp4")

    assert refined["segments"][0]["start"] == 10.0
    assert refined["segments"][0]["end"] == 30.0
    assert "segment_duration:span_01" in refined["local_scene_alignment"]["revert_reasons"]
