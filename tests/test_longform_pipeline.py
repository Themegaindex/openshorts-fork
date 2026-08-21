from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("ultralytics")
pytest.importorskip("mediapipe")
pytest.importorskip("google.genai")

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


def test_longform_resume_reuses_valid_checkpoint_without_gemini(monkeypatch, tmp_path):
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
        _transcript(), 900, output_dir=str(tmp_path), video_title="Video", resume_phase="render",
    )
    assert result == checkpoint


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
