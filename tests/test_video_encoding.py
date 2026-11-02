import subprocess
import threading

import pytest

import video_encoding as ve


@pytest.fixture
def gpu(monkeypatch):
    """NVENC 'available' through a fake probe; counts probe calls per profile."""
    calls = {"full": 0, "compat": 0}
    state = {"full": True, "compat": True}

    def fake_probe(profile):
        calls[profile] += 1
        return state[profile]

    monkeypatch.setenv("VIDEO_ENCODER", "auto")
    monkeypatch.setattr(ve, "_run_probe", fake_probe)
    ve.reset_state()
    return {"calls": calls, "state": state}


def _no_probe(_profile):
    raise AssertionError("the GPU probe must not run")


def test_x264_mode_pins_legacy_args_for_every_tier(monkeypatch):
    monkeypatch.setattr(ve, "_run_probe", _no_probe)
    assert ve.video_encode_args(ve.MASTER) == ["-c:v", "libx264", "-preset", "slow", "-crf", "18"]
    assert ve.video_encode_args(ve.HIGH) == ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
    assert ve.video_encode_args(ve.FINAL) == ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    assert ve.video_encode_args(ve.STANDARD) == ["-c:v", "libx264", "-preset", "fast", "-crf", "22"]
    assert ve.video_encode_args(ve.REFRAME) == ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    assert [a.encoder for a in ve.encode_attempts(ve.REFRAME, width=1080, height=1920)] == ["x264"]


def test_auto_uses_full_nvenc_profile_when_probe_passes(gpu):
    args = ve.video_encode_args(ve.MASTER)
    assert args[:2] == ["-c:v", "h264_nvenc"]
    joined = " ".join(args)
    for fragment in ("-profile:v high", "-rc vbr", "-b:v 0", "-temporal-aq 1", "-b_ref_mode middle",
                     f"-cq {ve._NVENC_TIERS[ve.MASTER][2]}"):
        assert fragment in joined
    assert args[-2:] == ["-pix_fmt", "yuv420p"]


def test_compat_profile_when_only_basic_nvenc_works(gpu):
    gpu["state"]["full"] = False
    args = " ".join(ve.video_encode_args(ve.HIGH))
    assert "h264_nvenc" in args and "-profile:v high" in args and args.endswith("-pix_fmt yuv420p")
    for fragment in ("-temporal-aq", "-b_ref_mode", "-rc-lookahead", "-multipass"):
        assert fragment not in args


@pytest.mark.parametrize("error", [FileNotFoundError("ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 20)])
def test_probe_failure_missing_ffmpeg_or_timeout_means_x264(monkeypatch, error):
    monkeypatch.setenv("VIDEO_ENCODER", "nvenc")
    ve.reset_state()

    def broken_run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(ve.subprocess, "run", broken_run)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **_k: printed.append(" ".join(map(str, a))))
    assert ve.select_encoder() == "x264"
    assert ve.select_encoder() == "x264"
    assert sum("test encode failed" in line for line in printed) == 1


def test_probe_runs_once_across_threads(gpu):
    threads = [threading.Thread(target=ve.video_encode_args, args=(ve.HIGH,)) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert gpu["calls"]["full"] == 1


def test_size_limits_force_x264(gpu):
    assert ve.select_encoder(1080, 1920) == "nvenc"
    assert ve.select_encoder(4096, 2304) == "nvenc"
    for size in ((100, 100), (144, 1920), (4160, 2160)):
        assert ve.select_encoder(*size) == "x264"


def test_cq_offset_shifts_and_clamps(gpu, monkeypatch):
    base = ve._NVENC_TIERS[ve.MASTER][2]
    monkeypatch.setenv("VIDEO_NVENC_CQ_OFFSET", "-3")
    args = ve.video_encode_args(ve.MASTER)
    assert args[args.index("-cq") + 1] == str(base - 3)
    monkeypatch.setenv("VIDEO_NVENC_CQ_OFFSET", "99")
    args = ve.video_encode_args(ve.MASTER)
    assert args[args.index("-cq") + 1] == "51"


def test_run_encode_x264_runs_once_and_returns_runner_result(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return None

    result = ve.run_encode(
        lambda attempt: ["ffmpeg", *attempt.video_args, "out.mp4"], tier=ve.FINAL, label="t",
        output_path=str(tmp_path / "never-written.mp4"), runner=runner, timeout=5,
    )
    assert result is None
    assert len(calls) == 1 and calls[0][1] == {"check": True, "timeout": 5}
    assert "libx264" in calls[0][0]


def test_run_encode_reruns_failed_nvenc_with_x264_same_other_args(gpu, tmp_path):
    out = tmp_path / "out.mp4"
    commands, warnings = [], []

    def runner(command, **_kwargs):
        commands.append(command)
        if "h264_nvenc" in command:
            raise subprocess.CalledProcessError(1, command, stderr=b"[h264_nvenc] OpenEncodeSessionEx failed: out of memory (10)")
        out.write_bytes(b"x" * 5000)

    ve.run_encode(
        lambda attempt: ["ffmpeg", "-i", "in.mp4", *attempt.video_args, str(out)],
        tier=ve.REFRAME, label="Burn", output_path=str(out), runner=runner, warn=warnings.append,
    )
    assert ["h264_nvenc" in c for c in commands] == [True, False]
    assert commands[1][:3] == commands[0][:3] and commands[1][-1] == commands[0][-1]
    assert len(warnings) == 1 and "out of memory" in warnings[0] and "libx264" in warnings[0]


def test_run_encode_treats_exit0_empty_output_as_nvenc_failure(gpu, tmp_path):
    out = tmp_path / "out.mp4"
    encoders = []

    def runner(command, **_kwargs):
        encoders.append("nvenc" if "h264_nvenc" in command else "x264")
        out.write_bytes(b"" if "h264_nvenc" in command else b"x" * 5000)

    ve.run_encode(lambda a: ["ffmpeg", *a.video_args, str(out)], tier=ve.HIGH, label="Cut",
                  output_path=str(out), runner=runner, warn=lambda _m: None)
    assert encoders == ["nvenc", "x264"]


def test_run_encode_never_retries_timeouts(gpu):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 5)

    with pytest.raises(subprocess.TimeoutExpired):
        ve.run_encode(lambda a: ["ffmpeg", *a.video_args], tier=ve.HIGH, label="Cut", runner=runner)
    assert len(calls) == 1


def test_breaker_latches_after_three_failures_and_expires(gpu, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(ve.time, "monotonic", lambda: clock["now"])
    ve.note_nvenc_failure("a")
    ve.note_nvenc_success()
    ve.note_nvenc_failure("b")
    ve.note_nvenc_failure("c")
    assert ve.select_encoder() == "nvenc"
    ve.note_nvenc_failure("d")
    assert ve.select_encoder() == "x264"
    clock["now"] += ve.LATCH_SECONDS + 1
    probes_before = gpu["calls"]["full"]
    assert ve.select_encoder() == "nvenc"
    assert gpu["calls"]["full"] == probes_before + 1


def test_device_error_reprobes_before_latching(gpu):
    ve.nvenc_profile()
    ve.note_nvenc_failure("No capable devices found")
    assert ve.select_encoder() == "nvenc"          # oversized frame: GPU still fine
    ve.reset_state()
    gpu["state"]["full"] = gpu["state"]["compat"] = True
    ve.nvenc_profile()
    gpu["state"]["full"] = gpu["state"]["compat"] = False
    ve.note_nvenc_failure("No capable devices found")
    assert ve.select_encoder() == "x264"


_DEAD_GPU_STDERR = (
    b"Input #0, mov,mp4 from 'in.mp4':\n"
    b"[h264_nvenc @ 000001] No capable devices found\n"
    b"[vost#0:0/h264_nvenc @ 000002] Error while opening encoder\n"
    b"Conversion failed!\n"
)


def test_short_reason_prefers_the_gpu_line_over_ffmpegs_last_line():
    error = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=_DEAD_GPU_STDERR)
    assert ve.short_reason(error) == "[vost#0:0/h264_nvenc @ 000002] Error while opening encoder"
    assert ve.short_reason(b"plain\nConversion failed!\n") == "Conversion failed!"
    assert "No capable devices found" in ve.failure_text(error)


def test_run_encode_latches_at_once_when_the_gpu_died(gpu, tmp_path):
    ve.nvenc_profile()
    gpu["state"]["full"] = gpu["state"]["compat"] = False      # re-probe fails: device gone
    out = tmp_path / "out.mp4"
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if "h264_nvenc" in command:
            raise subprocess.CalledProcessError(1, command, stderr=_DEAD_GPU_STDERR)
        out.write_bytes(b"x" * 5000)

    ve.run_encode(lambda a: ["ffmpeg", *a.video_args, str(out)], tier=ve.HIGH, label="Cut",
                  output_path=str(out), runner=runner, warn=lambda _m: None)
    assert ["h264_nvenc" in c for c in calls] == [True, False]
    assert ve.select_encoder() == "x264"


def test_nvdec_attempt_chain_for_av1_source(gpu, monkeypatch):
    monkeypatch.setenv("VIDEO_HWDECODE", "auto")
    monkeypatch.setattr(ve, "_probe_source", lambda _p: {"codec": "av1", "pix_fmt": "yuv420p", "rotation": 0, "cpu_only": False})
    attempts = ve.encode_attempts(ve.HIGH, source="s.mp4", gpu_frames=True)
    assert [a.label for a in attempts] == ["h264_nvenc+NVDEC", "h264_nvenc", "libx264"]
    assert list(attempts[0].input_args) == ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", "av1"]
    assert "-pix_fmt" not in attempts[0].video_args
    assert "-pix_fmt" in attempts[1].video_args


def test_nvdec_skips_10bit_rotation_and_off_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("VIDEO_HWDECODE", "auto")
    info = {"codec": "hevc", "pix_fmt": "yuv420p10le", "rotation": 0, "cpu_only": False}
    monkeypatch.setattr(ve, "_probe_source", lambda _p: info)
    assert ve.nvdec_input_args("s.mp4", gpu_frames=True) == []
    assert ve.nvdec_input_args("s.mp4", gpu_frames=False) == ["-hwaccel", "cuda"]
    info["rotation"] = 90
    assert ve.nvdec_input_args("s.mp4", gpu_frames=False) == []
    info["rotation"] = 0
    monkeypatch.setenv("VIDEO_HWDECODE", "off")
    assert ve.nvdec_input_args("s.mp4", gpu_frames=False) == []
    monkeypatch.setenv("VIDEO_HWDECODE", "auto")
    ve.mark_source_cpu_decode("s.mp4")
    assert ve.nvdec_input_args("s.mp4", gpu_frames=False) == []


def test_builders_match_legacy_commands():
    cut = ve.clip_cut_command("src.mp4", 1.5, 9.0, "temp.mp4", video_args=ve._X264_ARGS[ve.HIGH])
    assert cut == [
        "ffmpeg", "-y", "-ss", "1.5", "-to", "9.0", "-i", "src.mp4",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "aac", "temp.mp4",
    ]
    pipe = ve.reframe_pipe_command(608, 1080, 25, "t.mp4", video_args=ve._X264_ARGS[ve.REFRAME])
    assert pipe == [
        "ffmpeg", "-y", "-nostats", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-s", "608x1080", "-pix_fmt", "bgr24",
        "-r", "25", "-i", "-", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an", "t.mp4",
    ]
