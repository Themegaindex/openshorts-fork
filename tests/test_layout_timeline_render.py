"""The renderer records which layout each scene got, next to the final clip."""
import json
import pathlib
import shutil
import subprocess

import pytest

cv2 = pytest.importorskip("cv2", reason="OpenCV integration tests run in the video environment")
np = pytest.importorskip("numpy")
pytest.importorskip("ultralytics")
pytest.importorskip("mediapipe")

import caption_placement as cp  # noqa: E402
import main  # noqa: E402
from render_planning import SceneLayoutDecision  # noqa: E402


SEGMENTS = [
    cp.LayoutSegment(0.0, 1.0, cp.LAYOUT_SPLIT_STACKED),
    cp.LayoutSegment(1.0, 2.5, cp.LAYOUT_TRACK),
]


def _renderer(segments=None, success=True):
    def render(input_video, output_path, *args, layout_record=None, **kwargs):
        if not success:
            return False
        pathlib.Path(output_path).write_bytes(b"video")
        if segments is not None and layout_record is not None:
            layout_record["fps"] = 30.0
            layout_record["segments"] = list(segments)
        return True
    return render


def test_timeline_is_written_under_the_final_name_not_the_partial_one(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "process_video_to_vertical", _renderer(SEGMENTS))
    final = tmp_path / "show_clip_1.mp4"
    assert main._render_clip("in.mp4", str(final), output_format="vertical") is True
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "show_clip_1.mp4", "show_clip_1.mp4.layout.json",
    ]
    assert cp.load_layout_timeline(str(final)) == SEGMENTS


def test_a_render_without_timeline_removes_a_stale_one(monkeypatch, tmp_path):
    final = tmp_path / "clip.mp4"
    sidecar = pathlib.Path(cp.layout_sidecar_path(str(final)))
    sidecar.write_text(json.dumps(cp.layout_timeline_payload(SEGMENTS, 30)), encoding="utf-8")
    # Source already 9:16: process_video_to_vertical passes it through.
    monkeypatch.setattr(main, "process_video_to_vertical", _renderer(None))
    assert main._render_clip("in.mp4", str(final), output_format="vertical") is True
    assert not sidecar.exists()

    sidecar.write_text(json.dumps(cp.layout_timeline_payload(SEGMENTS, 30)), encoding="utf-8")
    monkeypatch.setattr(main, "_finalize_clip_passthrough", _renderer(None))
    assert main._render_clip("in.mp4", str(final), output_format="original") is True
    assert not sidecar.exists()


def test_a_failed_render_keeps_the_previous_clip_and_its_timeline(monkeypatch, tmp_path):
    final = tmp_path / "clip.mp4"
    final.write_bytes(b"old video")
    sidecar = pathlib.Path(cp.layout_sidecar_path(str(final)))
    sidecar.write_text(json.dumps(cp.layout_timeline_payload(SEGMENTS, 30)), encoding="utf-8")
    monkeypatch.setattr(main, "process_video_to_vertical", _renderer(SEGMENTS[:1], success=False))
    assert main._render_clip("in.mp4", str(final), output_format="vertical") is False
    assert final.read_bytes() == b"old video"
    assert cp.load_layout_timeline(str(final)) == SEGMENTS


def test_a_failed_timeline_write_does_not_fail_the_render(monkeypatch, tmp_path):
    final = tmp_path / "clip.mp4"
    sidecar = pathlib.Path(cp.layout_sidecar_path(str(final)))
    sidecar.write_text("stale", encoding="utf-8")

    def broken_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(main, "process_video_to_vertical", _renderer(SEGMENTS))
    monkeypatch.setattr(main, "_save_json_file", broken_save)
    assert main._render_clip("in.mp4", str(final), output_format="vertical") is True
    assert final.read_bytes() == b"video"
    assert not sidecar.exists()   # never a timeline of an earlier render


class _FrameNumber:
    def __init__(self, value):
        self.value = value

    def get_frames(self):
        return self.value


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe",
)
def test_the_cpu_rerender_records_the_layout_the_frames_got(monkeypatch, tmp_path):
    """A stacked SPLIT is recorded as such, a SPLIT without centers as the
    GENERAL frame it is drawn as, also when a dead GPU encoder forces the
    x264 second pass."""
    import video_encoding

    source = tmp_path / "wide.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True)

    real_attempts = video_encoding.encode_attempts

    def attempts(tier, **kwargs):
        if kwargs.get("encoder") == "x264":
            return real_attempts(tier, **kwargs)
        return [video_encoding.EncodeAttempt("nvenc", ("-c:v", "libx264", "-no_such_option", "1"))]

    decisions = [
        SceneLayoutDecision("SPLIT", [(160, 180), (480, 180)], 0.95, "two people"),
        SceneLayoutDecision("SPLIT", None, 0.95, "no centers"),
    ]
    monkeypatch.setattr(main, "detect_scenes", lambda _path: (
        [(_FrameNumber(0), _FrameNumber(25)), (_FrameNumber(25), _FrameNumber(50))], 25.0,
    ))
    monkeypatch.setattr(main, "analyze_scenes_strategy", lambda *_args, **_kwargs: list(decisions))
    monkeypatch.setattr(main.video_encoding, "encode_attempts", attempts)
    gpu_failures = []
    monkeypatch.setattr(main.video_encoding, "note_nvenc_failure", gpu_failures.append)
    monkeypatch.setattr(main, "WATERMARK_ENABLED", False)

    final = tmp_path / "clip.mp4"
    assert main._render_clip(str(source), str(final), output_format="vertical") is True
    assert len(gpu_failures) == 1   # the timeline below comes from the x264 second pass
    assert cp.load_layout_timeline(str(final)) == [
        cp.LayoutSegment(0.0, 1.0, cp.LAYOUT_SPLIT_STACKED),
        cp.LayoutSegment(1.0, 2.0, cp.LAYOUT_GENERAL),
    ]
    assert not pathlib.Path(cp.layout_sidecar_path(main._partial_render_path(str(final)))).exists()


def _two_colour_source(tmp_path):
    """One red second, one blue second, 640x360 at 25 fps."""
    source = tmp_path / "two_scenes.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=640x360:r=25:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True)
    return source


def _track_two_scenes(monkeypatch, gpu_dies=False):
    """Two TRACK scenes: a face in the red one, only a YOLO person in the blue one."""
    import video_encoding

    def faces(frame):
        red_scene = frame[..., 2].mean() > frame[..., 0].mean()   # BGR
        return [{"box": [300, 216, 40, 54], "score": 2160}] if red_scene else []

    monkeypatch.setattr(main, "detect_scenes", lambda _path: (
        [(_FrameNumber(0), _FrameNumber(25)), (_FrameNumber(25), _FrameNumber(50))], 25.0,
    ))
    monkeypatch.setattr(main, "analyze_scenes_strategy", lambda *_a, **_k: [
        SceneLayoutDecision("TRACK", None, 0.95, "one face"),
        SceneLayoutDecision("TRACK", None, 0.95, "one person"),
    ])
    monkeypatch.setattr(main, "detect_face_candidates", faces)
    person_calls = []

    def person(_frame):
        person_calls.append(1)
        return [250, 40, 140, 300]

    monkeypatch.setattr(main, "detect_person_yolo", person)
    monkeypatch.setattr(main, "WATERMARK_ENABLED", False)
    failures = []
    if gpu_dies:
        real_attempts = video_encoding.encode_attempts

        def attempts(tier, **kwargs):
            if kwargs.get("encoder") == "x264":
                return real_attempts(tier, **kwargs)
            return [video_encoding.EncodeAttempt("nvenc", ("-c:v", "libx264", "-no_such_option", "1"))]

        monkeypatch.setattr(main.video_encoding, "encode_attempts", attempts)
        monkeypatch.setattr(main.video_encoding, "note_nvenc_failure", failures.append)
    return failures, person_calls


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe",
)
@pytest.mark.parametrize("gpu_dies", [False, True])
def test_the_renderer_records_where_the_tracked_face_is_drawn(monkeypatch, tmp_path, gpu_dies):
    source = _two_colour_source(tmp_path)
    failures, person_calls = _track_two_scenes(monkeypatch, gpu_dies=gpu_dies)
    final = tmp_path / "clip.mp4"
    assert main._render_clip(str(source), str(final), output_format="vertical") is True
    assert len(failures) == (1 if gpu_dies else 0)   # the x264 second pass records the same
    timeline = cp.load_layout_timeline(str(final))
    assert [segment.layout for segment in timeline] == [cp.LAYOUT_TRACK, cp.LAYOUT_TRACK]
    # The 9:16 window keeps the full 360 px height: the face at y 216-270 sits
    # at 60-75 % of the drawn frame.
    assert timeline[0].face_top == pytest.approx(0.60, abs=0.002)
    assert timeline[0].face_bottom == pytest.approx(0.75, abs=0.002)
    # The YOLO person box of the blue scene is no face.
    assert person_calls and not timeline[1].has_face


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe",
)
def test_a_broken_face_sample_never_stops_the_render(monkeypatch, tmp_path):
    source = _two_colour_source(tmp_path)
    _track_two_scenes(monkeypatch)

    def broken(*_args, **_kwargs):
        raise RuntimeError("bad face box")

    monkeypatch.setattr(main.caption_placement, "face_band_in_crop", broken)
    final = tmp_path / "clip.mp4"
    assert main._render_clip(str(source), str(final), output_format="vertical") is True
    timeline = cp.load_layout_timeline(str(final))
    assert len(timeline) == 2 and not any(segment.has_face for segment in timeline)
