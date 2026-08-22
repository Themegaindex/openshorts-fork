from contextlib import contextmanager
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
    assert json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8")) == metadata
    assert not any(event[0] == "error" for event in reporter.events)


def test_auto_keeps_shorts_when_long_video_render_fails(monkeypatch, tmp_path):
    render_order = []

    def render_shorts(*_args, **_kwargs):
        render_order.append("shorts")
        return 30.0

    def fail_long(*_args, **_kwargs):
        render_order.append("long")
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
        "attempts": [{"stage": "longform_plan", "status": "success"}],
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


def test_score_fallback_builds_bounded_chronological_story():
    windows = [
        {"id": f"window_{index:03d}", "start": index * 90, "end": (index + 1) * 90, "text": "text"}
        for index in range(10)
    ]
    scores = [
        {"id": item["id"], "start": item["start"], "end": item["end"], "score": 100 - index, "reason": ""}
        for index, item in enumerate(windows)
    ]

    plan = main._score_based_longform_fallback(
        _transcript(), 900, video_title="Example", windows=windows, scored_windows=scores,
    )

    assert plan["viable"] is True
    assert 480 <= plan["total_duration"] <= 600
    assert plan["segments"][0]["role"] == "cold_open"
    body = plan["segments"][1:]
    assert [item["start"] for item in body] == sorted(item["start"] for item in body)
    assert all(item["end"] - item["start"] <= main.LONGFORM_MAX_SEGMENT_SECONDS + 0.001 for item in body)
    assert [item["chapter_title"] for item in body] == [f"Teil {index}" for index in range(1, len(body) + 1)]
    assert plan["youtube_description"] == ""
    assert "score_based_fallback" in plan["warnings"]
    assert "fewer_than_three_chapters" not in plan["warnings"]


def test_score_fallback_does_not_pad_with_unscored_windows():
    windows = [
        {"id": f"window_{index:03d}", "start": index * 90, "end": (index + 1) * 90, "text": "text"}
        for index in range(10)
    ]
    only_one_score = [{
        "id": windows[0]["id"], "start": 0, "end": 90, "score": 100, "reason": "strong",
    }]

    plan = main._score_based_longform_fallback(
        _transcript(), 900, video_title="Example", windows=windows, scored_windows=only_one_score,
    )

    assert plan is None


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


def test_score_fallback_normalizes_distance_for_long_sources():
    selected = [{"start": 0, "end": 90, "score": 100}]
    far_strong = {"start": 3600, "end": 3690, "score": 95}
    near_weak = {"start": 120, "end": 210, "score": 5}

    assert main._score_fallback_selection_value(far_strong, selected, 7200) > (
        main._score_fallback_selection_value(near_weak, selected, 7200)
    )


@pytest.mark.parametrize("resume_phase", [None, "render"])
def test_longform_resume_reuses_valid_checkpoint_without_gemini(monkeypatch, tmp_path, resume_phase):
    reporter = _Reporter()
    monkeypatch.setattr(main, "JOB_REPORTER", reporter)
    checkpoint = {
        "plan_data": {
            "viable": True,
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
        _transcript(), 900, output_dir=str(tmp_path), video_title="Video",
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


def test_auto_threshold_considers_nine_minute_sources():
    assert main.LONGFORM_MIN_SOURCE_SECONDS <= 9 * 60
    target_min, target_max, _warnings = main._longform_target_range(9 * 60)
    assert target_min == 480
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
