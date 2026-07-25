"""Audio extraction must never silently drop a track that exists.

Copying the audio stream into an .aac container only works for AAC sources.
MKV/WebM uploads and some YouTube merges carry Opus, AC3 or Vorbis, where
FFmpeg fails with "adts muxer supports only codec aac for type audio" — and
the renderer used to continue anyway, shipping a clip with no sound at all.
"""
import subprocess

import pytest

from media_audio import (
    copy_audio_command,
    extract_audio_track,
    reencode_audio_command,
    source_has_audio_stream,
)


class _Result:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    """Fake subprocess.run that scripts an outcome per command kind."""

    def __init__(self, *, probe=b"audio", copy_ok=True, encode_ok=True,
                 copy_raises=None):
        self.probe = probe
        self.copy_ok = copy_ok
        self.encode_ok = encode_ok
        self.copy_raises = copy_raises
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'ffprobe':
            return _Result(stdout=self.probe)
        if '-acodec' in command:  # stream copy
            if self.copy_raises:
                raise self.copy_raises
            if self.copy_ok:
                return _Result()
            return _Result(1, stderr=b"adts muxer supports only codec aac for type audio")
        if self.encode_ok:
            return _Result()
        return _Result(1, stderr=b"Invalid data found when processing input")


@pytest.fixture
def audio_file(tmp_path):
    """A path whose file appears once an attempt is meant to succeed."""
    return tmp_path / "audio.aac"


def _writing(runner, path):
    """Wrap a runner so a successful ffmpeg attempt also produces the file."""
    def wrapper(command, **kwargs):
        result = runner(command, **kwargs)
        if command[0] == 'ffmpeg' and result.returncode == 0:
            path.write_bytes(b"audio-payload")
        return result

    return wrapper


class TestSourceHasAudioStream:
    def test_detects_present_stream(self):
        assert source_has_audio_stream("in.mp4", runner=_Runner()) is True

    def test_detects_missing_stream(self):
        assert source_has_audio_stream("in.mp4", runner=_Runner(probe=b"")) is False

    def test_probe_failure_is_unknown_not_absent(self):
        """Unknown must not be treated as 'no audio' — that would skip extraction."""
        def runner(command, **kwargs):
            return _Result(returncode=1)
        assert source_has_audio_stream("in.mp4", runner=runner) is None

    def test_probe_exception_is_unknown(self):
        def runner(command, **kwargs):
            raise OSError("ffprobe missing")
        assert source_has_audio_stream("in.mp4", runner=runner) is None


class TestExtractAudioTrack:
    def test_stream_copy_is_preferred(self, audio_file):
        runner = _Runner()
        assert extract_audio_track(
            "in.mp4", str(audio_file), log=lambda *_: None,
            runner=_writing(runner, audio_file),
        ) is True
        # Copy succeeded, so no re-encode is attempted.
        assert runner.commands[-1] == copy_audio_command("in.mp4", str(audio_file))
        assert not any('-c:a' in command for command in runner.commands)

    def test_opus_source_falls_back_to_aac_instead_of_going_silent(self, audio_file):
        """The regression: copy fails, and the clip used to end up muted."""
        runner = _Runner(copy_ok=False)
        warnings = []
        assert extract_audio_track(
            "in.mkv", str(audio_file), log=lambda *_: None,
            warn=warnings.append, runner=_writing(runner, audio_file),
        ) is True
        assert audio_file.read_bytes() == b"audio-payload"
        assert warnings == []  # audio was saved, nothing to warn about
        assert runner.commands[-1] == reencode_audio_command("in.mkv", str(audio_file))

    def test_timeout_on_copy_still_tries_the_reencode(self, audio_file):
        runner = _Runner(copy_raises=subprocess.TimeoutExpired("ffmpeg", 1800))
        assert extract_audio_track(
            "in.mkv", str(audio_file), log=lambda *_: None,
            runner=_writing(runner, audio_file),
        ) is True

    def test_empty_output_counts_as_failure(self, audio_file):
        """FFmpeg can exit 0 and leave a zero-byte file for an unusable track."""
        runner = _Runner()  # both attempts "succeed" but nothing writes the file
        warnings = []
        assert extract_audio_track(
            "in.mp4", str(audio_file), log=lambda *_: None,
            warn=warnings.append, runner=runner,
        ) is False
        assert len(warnings) == 1

    def test_total_failure_warns_the_user(self, audio_file):
        runner = _Runner(copy_ok=False, encode_ok=False)
        warnings = []
        assert extract_audio_track(
            "in.mkv", str(audio_file), log=lambda *_: None,
            warn=warnings.append, runner=runner,
        ) is False
        assert "no sound" in warnings[0]
        assert not audio_file.exists()

    def test_silent_source_is_not_an_error(self, audio_file):
        """No audio track at all is a normal outcome, not something to warn about."""
        runner = _Runner(probe=b"")
        warnings = []
        assert extract_audio_track(
            "in.mp4", str(audio_file), log=lambda *_: None,
            warn=warnings.append, runner=runner,
        ) is False
        assert warnings == []
        # No extraction attempt is made at all.
        assert all(c[0] == 'ffprobe' for c in runner.commands)

    def test_unknown_probe_result_still_attempts_extraction(self, audio_file):
        """A failing probe must not be mistaken for 'this file has no audio'."""
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] == 'ffprobe':
                return _Result(returncode=1)
            audio_file.write_bytes(b"audio-payload")
            return _Result()

        assert extract_audio_track(
            "in.mp4", str(audio_file), log=lambda *_: None, runner=runner,
        ) is True
        assert calls[1] == copy_audio_command("in.mp4", str(audio_file))
