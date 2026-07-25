"""Audio-track extraction that survives sources FFmpeg cannot stream-copy.

A stream copy into an ``.aac`` (ADTS) container only works when the source
audio is already AAC. Uploaded MKV/WebM files and some YouTube merges carry
Opus, AC3 or Vorbis, where the copy fails with::

    adts muxer supports only codec aac for type audio

The renderer used to log one line and continue, silently shipping a CLIP
WITHOUT SOUND. These helpers fall back to an AAC re-encode instead, and only
report a real problem when both attempts fail.

Like clip_selection/render_planning/video_formats this module is stdlib only,
so the behaviour is unit-testable without the video/ML stack.
"""

import os
import subprocess

AUDIO_TIMEOUT_SECONDS = 1800
AAC_FALLBACK_BITRATE = "192k"


def probe_audio_stream_command(video_path):
    return [
        'ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', video_path,
    ]


def copy_audio_command(input_video, output_path):
    return ['ffmpeg', '-y', '-i', input_video, '-vn', '-acodec', 'copy', output_path]


def reencode_audio_command(input_video, output_path):
    return [
        'ffmpeg', '-y', '-i', input_video, '-vn',
        '-c:a', 'aac', '-b:a', AAC_FALLBACK_BITRATE, output_path,
    ]


def source_has_audio_stream(video_path, runner=subprocess.run):
    """True/False if ffprobe could decide, None when the probe itself failed."""
    try:
        result = runner(
            probe_audio_stream_command(video_path),
            capture_output=True, timeout=AUDIO_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    stdout = result.stdout or b""
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8", errors="replace")
    return b"audio" in stdout


def _attempt(command, output_path, runner):
    """Run one extraction attempt. Returns (ok, reason_when_not_ok)."""
    try:
        result = runner(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=AUDIO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {AUDIO_TIMEOUT_SECONDS}s"
    if result.returncode != 0:
        stderr = result.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        last_line = stderr.strip().splitlines()[-1:] or ["unknown error"]
        return False, last_line[0].strip()
    # FFmpeg can exit 0 and still leave an empty file for an unusable track.
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        return False, "produced an empty file"
    return True, None


def extract_audio_track(input_video, output_path, *, log=print, warn=None,
                        runner=subprocess.run):
    """Extract audio into ``output_path``, re-encoding to AAC when required.

    Returns True when ``output_path`` holds usable audio. A source with no
    audio track at all is a normal outcome and returns False without warning;
    losing an audio track that *does* exist raises a job warning, because that
    silently produced muted clips before.
    """
    if source_has_audio_stream(input_video, runner=runner) is False:
        log("   ℹ️ Source has no audio stream — rendering a silent clip.")
        return False

    copied, copy_error = _attempt(
        copy_audio_command(input_video, output_path), output_path, runner,
    )
    if copied:
        return True

    log(f"   ⚠️ Audio stream copy failed ({copy_error}); re-encoding to AAC...")
    encoded, encode_error = _attempt(
        reencode_audio_command(input_video, output_path), output_path, runner,
    )
    if encoded:
        log("   ✅ Audio re-encoded to AAC.")
        return True

    if os.path.exists(output_path):
        os.remove(output_path)
    message = (
        "Audio could not be extracted from this clip — it will have no sound. "
        f"Stream copy and AAC re-encode both failed ({encode_error})."
    )
    log(f"   ❌ {message}")
    if warn:
        warn(message)
    return False
