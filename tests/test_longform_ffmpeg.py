import json
import shutil
import subprocess

import pytest

import longform


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg integration test requires ffmpeg and ffprobe",
)


def _run(command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)


def test_four_by_three_segments_become_synced_16_9_h264_aac_video(tmp_path):
    source = tmp_path / "source_4x3.mp4"
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(source),
    ])

    filter_graph = longform.blurred_16_9_filter(320, 180)
    segment_paths = [tmp_path / "part_1.mp4", tmp_path / "part_2.mp4"]
    for (start, end), output in zip(((0.5, 2.5), (3.0, 5.0)), segment_paths):
        command = longform.segment_cut_command(
            source, start, end, output, filter_complex=filter_graph,
        )
        assert "d=0.010" in command[command.index("-af") + 1]
        _run(command)

    manifest = tmp_path / "manifest.txt"
    manifest.write_text(longform.concat_manifest_text(segment_paths), encoding="utf-8")
    joined = tmp_path / "joined.mp4"
    _run(longform.concat_command(manifest, joined))

    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(joined),
    ], check=True, capture_output=True, text=True, timeout=30)
    payload = json.loads(probe.stdout)
    video = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in payload["streams"] if stream["codec_type"] == "audio")

    assert (video["width"], video["height"]) == (320, 180)
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert 3.85 <= float(payload["format"]["duration"]) <= 4.15
    assert abs(float(video["duration"]) - float(audio["duration"])) < 0.15

    # +faststart moves MP4 metadata ahead of the media payload for web playback.
    header = joined.read_bytes()[:100_000]
    assert header.find(b"moov") < header.find(b"mdat")


def test_exact_16_9_source_without_audio_still_cuts_successfully(tmp_path):
    source = tmp_path / "silent_16x9.mp4"
    output = tmp_path / "silent_cut.mp4"
    _run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ])

    _run(longform.segment_cut_command(source, 0.25, 1.75, output))
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
    ], check=True, capture_output=True, text=True, timeout=30)
    streams = json.loads(probe.stdout)["streams"]

    assert [stream["codec_type"] for stream in streams] == ["video"]
    assert (streams[0]["width"], streams[0]["height"]) == (320, 180)
