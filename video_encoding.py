"""Central video encoder policy: NVIDIA NVENC when it works, libx264 otherwise.

Every H.264 encode in the app picks its arguments here. VIDEO_ENCODER=auto
uses h264_nvenc when a tiny cached test encode succeeds, and any failed NVENC
run is redone with the unchanged libx264 arguments, so a missing or busy GPU
never fails a job. Optional NVDEC decoding is used for encodes that read the
large downloaded source. Ideas adapted from upstream openshorts
ffmpeg_utils.py (MIT), tuned for quality instead of file size.

Stdlib-only at import time so CI can import it without ffmpeg or a GPU.
"""
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass

# Encode tiers, named by role.
MASTER = "master"        # burned subtitles and neon looks (final picture)
HIGH = "high"            # clip-cut and long-form segment intermediates
FINAL = "final"          # watermarked passthrough finalize
STANDARD = "standard"    # AI editor, SaaS shorts
REFRAME = "reframe"      # vertical/square reframe pipe, hook-only burns

# libx264 arguments, byte-identical to the commands before NVENC existed.
_X264_ARGS = {
    MASTER: ("-c:v", "libx264", "-preset", "slow", "-crf", "18"),
    HIGH: ("-c:v", "libx264", "-crf", "18", "-preset", "fast"),
    FINAL: ("-c:v", "libx264", "-preset", "fast", "-crf", "20"),
    STANDARD: ("-c:v", "libx264", "-preset", "fast", "-crf", "22"),
    REFRAME: ("-c:v", "libx264", "-preset", "fast", "-crf", "23"),
}
# (preset, multipass, cq) per tier. NVENC -cq is not on the x264 CRF scale.
# Calibrated with VMAF on a 4K source cropped to 1080x1920 (RTX 3070 Laptop):
#   master   x264 slow crf18 96.7 | nvenc p7 cq21 97.3
#   high     x264 fast crf18 96.5 | nvenc p5 cq19 97.5
#   final    x264 fast crf20 95.9 | nvenc p6 cq22 97.1
#   standard x264 fast crf22 95.2 | nvenc p5 cq24 96.7
#   reframe  x264 fast crf23 94.8 | nvenc p6 cq24 96.7
# Every tier is at least as good as the libx264 output it replaces, and
# encodes 2-3x faster; files are larger.
_NVENC_TIERS = {
    MASTER: ("p7", "fullres", 21),
    HIGH: ("p5", "qres", 19),
    FINAL: ("p6", "qres", 22),
    STANDARD: ("p5", "qres", 24),
    REFRAME: ("p6", "qres", 24),
}
# Verified on an RTX 3070: 144x144 is refused, 160x160 works, 4096x2304 works,
# 4160x2160 is refused ("No capable devices found").
NVENC_MIN_W, NVENC_MIN_H, NVENC_MAX_DIM = 146, 50, 4096
LATCH_SECONDS = 600
_DEVICE_MARKERS = (
    "No capable devices found", "Cannot load", "nvcuda",
    "Driver does not support the required nvenc API",
)
_HWDEC_CODECS = {"h264", "hevc", "vp9", "av1"}

_lock = threading.RLock()
_profile = None          # None = not probed yet, "" = unavailable, "full"/"compat"
_failures = 0
_latched_until = 0.0
_source_cache = {}
_warned = set()


class _EmptyOutput(Exception):
    pass


def _int_env(name, default):
    try:
        return int(str(os.environ.get(name, default)).strip())
    except ValueError:
        return int(default)


def _warn_once(message, warn=None):
    with _lock:
        if message in _warned:
            return
        _warned.add(message)
    _emit(warn, message)


def _emit(warn, message):
    if warn:
        try:
            warn(message)
            return
        except Exception:
            pass
    print(message)


def encoder_mode():
    raw = os.environ.get("VIDEO_ENCODER", "auto").strip().lower()
    if raw in ("x264", "libx264", "cpu", "off", "0"):
        return "x264"
    if raw in ("nvenc", "h264_nvenc", "gpu"):
        return "nvenc"
    return "auto"


def _nvenc_args(tier, profile):
    preset, multipass, cq = _NVENC_TIERS[tier]
    cq = min(51, max(0, cq + _int_env("VIDEO_NVENC_CQ_OFFSET", 0)))
    args = [
        "-c:v", "h264_nvenc", "-preset", preset, "-tune", "hq",
        # FFmpeg 8 defaults h264_nvenc to the Main profile; High enables the
        # 8x8 transform and matches libx264.
        "-profile:v", "high",
        "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
        "-spatial-aq", "1", "-aq-strength", "8",
    ]
    if profile == "full":
        args += [
            "-multipass", multipass, "-temporal-aq", "1", "-rc-lookahead", "20",
            "-bf", "3", "-b_ref_mode", "middle",
        ]
    # Mandatory: a bgr24 pipe would otherwise produce GBR-coded H.264 that
    # browsers show with swapped colours.
    return args + ["-pix_fmt", "yuv420p"]


def _run_probe(profile):
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=black:s=256x256:r=25:d=0.5",
        *_nvenc_args(MASTER, profile), "-f", "null", "-",
    ]
    try:
        return subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def nvenc_profile():
    """'full', 'compat' or None. The probe runs once per process."""
    global _profile
    with _lock:
        if _profile is None:
            if _run_probe("full"):
                _profile = "full"
            elif _run_probe("compat"):
                _profile = "compat"
            else:
                _profile = ""
            print(f"🎞️ [Encoder] h264_nvenc {_profile or 'unavailable'} (VIDEO_ENCODER={encoder_mode()})")
        return _profile or None


def nvenc_supports_size(width, height):
    return (
        NVENC_MIN_W <= int(width) <= NVENC_MAX_DIM
        and NVENC_MIN_H <= int(height) <= NVENC_MAX_DIM
    )


def select_encoder(width=None, height=None):
    """'nvenc' or 'x264' for an encode of the given output size."""
    global _latched_until, _profile
    mode = encoder_mode()
    if mode == "x264":
        return "x264"
    with _lock:
        if _latched_until:
            if time.monotonic() < _latched_until:
                return "x264"
            _latched_until = 0.0
            _profile = None          # latch expired: probe again
    if width and height and not nvenc_supports_size(width, height):
        return "x264"
    if nvenc_profile():
        return "nvenc"
    if mode == "nvenc":
        _warn_once("⚠️ VIDEO_ENCODER=nvenc but the h264_nvenc test encode failed; using libx264.")
    return "x264"


def video_encode_args(tier, encoder=None):
    if tier not in _X264_ARGS:
        raise ValueError(f"unknown encode tier {tier!r}")
    encoder = encoder or select_encoder()
    if encoder == "x264":
        return list(_X264_ARGS[tier])
    return _nvenc_args(tier, nvenc_profile() or "compat")


@dataclass(frozen=True)
class EncodeAttempt:
    encoder: str             # "nvenc" | "x264"
    video_args: tuple
    input_args: tuple = ()   # NVDEC args, placed before the source -i

    @property
    def label(self):
        if self.encoder == "x264":
            return "libx264"
        return "h264_nvenc+NVDEC" if self.input_args else "h264_nvenc"


def encode_attempts(tier, *, source=None, gpu_frames=False, width=None, height=None, encoder=None):
    """Ordered attempts for one encode: NVENC (+NVDEC) first, libx264 last."""
    x264 = EncodeAttempt("x264", tuple(_X264_ARGS[tier]))
    if encoder == "x264" or select_encoder(width, height) != "nvenc":
        return [x264]
    nvenc = tuple(video_encode_args(tier, "nvenc"))
    attempts = []
    decode = nvdec_input_args(source, gpu_frames=gpu_frames) if source else []
    if decode:
        attempts.append(EncodeAttempt(
            "nvenc", _strip_pix_fmt(nvenc) if gpu_frames else nvenc, tuple(decode),
        ))
    return attempts + [EncodeAttempt("nvenc", nvenc), x264]


def short_reason(value):
    """Last meaningful line of an ffmpeg error, for logs and warnings."""
    if isinstance(value, subprocess.CalledProcessError):
        text = value.stderr or value.output or b""
        if not text:
            return f"exit code {value.returncode}"
    elif isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    else:
        text = value or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return (lines[-1] if lines else "unknown error")[:200]


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_encode(build_command, *, tier, label, output_path=None, source=None, gpu_frames=False,
               width=None, height=None, runner=None, warn=None, **run_kwargs):
    """Run one encode with subprocess.run(..., check=True) semantics.

    build_command(attempt) returns the argv for an EncodeAttempt. A failed
    NVENC attempt (error exit or empty output) is rerun with the next attempt;
    the libx264 attempt behaves exactly like the old direct call. Timeouts are
    never retried.
    """
    attempts = encode_attempts(tier, source=source, gpu_frames=gpu_frames, width=width, height=height)
    for index, attempt in enumerate(attempts):
        run = runner or subprocess.run
        command = build_command(attempt)
        if attempt.encoder == "x264":
            return run(command, check=True, **run_kwargs)
        started = time.monotonic()
        try:
            result = run(command, check=True, **run_kwargs)
            if output_path and _size(output_path) < 1024:
                raise _EmptyOutput(f"{os.path.basename(output_path)} is empty after exit 0")
        except subprocess.TimeoutExpired:
            raise
        except (subprocess.CalledProcessError, _EmptyOutput, OSError) as exc:
            reason = short_reason(exc)
            if attempt.input_args and source:
                mark_source_cpu_decode(source)
            else:
                note_nvenc_failure(reason)
            _emit(warn, f"⚠️ {label}: {attempt.label} failed ({reason}); retrying with {attempts[index + 1].label}.")
            continue
        note_nvenc_success()
        print(f"🎞️ {label}: {attempt.label} in {time.monotonic() - started:.1f}s")
        return result
    raise RuntimeError(f"{label}: no encode attempt left")   # unreachable: x264 always returns or raises


def note_nvenc_failure(reason):
    """Count NVENC failures; after three (or a dead device) use libx264 for a while."""
    global _failures, _latched_until
    reason = str(reason or "")
    with _lock:
        _failures += 1
        trip = _failures >= 3
        profile = _profile
    if not trip and any(marker in reason for marker in _DEVICE_MARKERS):
        # Oversized frames print the same text, so only latch when the GPU
        # itself fails the test encode.
        trip = not _run_probe(profile or "compat")
    if trip:
        with _lock:
            _latched_until = time.monotonic() + LATCH_SECONDS
        _warn_once(f"⚠️ NVENC disabled for {LATCH_SECONDS // 60} min in this process: {reason}")


def note_nvenc_success():
    global _failures
    with _lock:
        _failures = 0


def _probe_source(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.realpath(path), stat.st_size, stat.st_mtime_ns)
    with _lock:
        if key in _source_cache:
            return _source_cache[key]
    info = None
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,pix_fmt:stream_side_data=rotation",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        stream = (json.loads(completed.stdout or "{}").get("streams") or [None])[0]
        if stream:
            rotation = next(
                (int(float(entry.get("rotation") or 0)) for entry in stream.get("side_data_list") or []),
                0,
            )
            info = {
                "codec": stream.get("codec_name"),
                "pix_fmt": stream.get("pix_fmt"),
                "rotation": rotation,
                "cpu_only": False,
            }
    except Exception:
        info = None
    with _lock:
        _source_cache[key] = info
    return info


def nvdec_input_args(source, *, gpu_frames):
    """Input args that decode the source on the GPU, or [] when not eligible."""
    if os.environ.get("VIDEO_HWDECODE", "auto").strip().lower() in ("off", "0", "cpu", "false"):
        return []
    info = _probe_source(source)
    if not info or info["cpu_only"] or info["codec"] not in _HWDEC_CODECS or info["rotation"] % 360:
        return []
    if gpu_frames and info["pix_fmt"] != "yuv420p":
        return []        # 10-bit frames cannot feed 8-bit h264_nvenc zero-copy
    args = ["-hwaccel", "cuda"]
    if gpu_frames:
        args += ["-hwaccel_output_format", "cuda"]
    if info["codec"] == "av1":
        # Older FFmpeg builds default to libdav1d, which ignores -hwaccel.
        args += ["-c:v", "av1"]
    return args


def _strip_pix_fmt(args):
    """CUDA frames are already NV12; -pix_fmt would force a failing software conversion."""
    items = list(args)
    index = items.index("-pix_fmt")
    del items[index:index + 2]
    return tuple(items)


def mark_source_cpu_decode(source):
    info = _probe_source(source)
    if info:
        info["cpu_only"] = True


def clip_cut_command(input_video, start, end, output_path, *, video_args, input_args=()):
    return [
        "ffmpeg", "-y", *input_args,
        "-ss", str(start), "-to", str(end), "-i", input_video,
        *video_args, "-c:a", "aac", output_path,
    ]


def reframe_pipe_command(width, height, fps, output_path, *, video_args):
    return [
        "ffmpeg", "-y", "-nostats", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{width}x{height}", "-pix_fmt", "bgr24",
        "-r", str(fps), "-i", "-", *video_args, "-pix_fmt", "yuv420p",
        "-an", output_path,
    ]


def reset_state():
    """Test hook: forget the probe result, failures, latch and source probes."""
    global _profile, _failures, _latched_until
    with _lock:
        _profile = None
        _failures = 0
        _latched_until = 0.0
        _source_cache.clear()
        _warned.clear()
