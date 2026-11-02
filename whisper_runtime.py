"""faster-whisper runtime shared by every transcription call site.

Picks the NVIDIA GPU when CTranslate2 sees one (large-v3-turbo, float16) and
falls back to the CPU (small, int8) when the GPU cannot be used. The policy is
adapted from upstream openshorts transcribe_backends.py (MIT), with Windows
error texts, a CPU-sized fallback model and explicit VRAM release.

Stdlib-only at import time: CI has neither ctranslate2 nor faster-whisper.
"""
import errno
import gc
import json
import os
import time

if os.name == "nt":
    import msvcrt
    # Windows without Developer Mode cannot symlink in the Hugging Face cache;
    # files are stored as copies and the warning is cosmetic.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
else:
    import fcntl

DEFAULT_CPU_MODEL = "small"
DEFAULT_GPU_MODEL = "large-v3-turbo"
CPU_COMPUTE_TYPE = "int8"
GPU_COMPUTE_PREFERENCE = ("float16", "int8_float16", "int8", "float32")

# Texts CTranslate2 raises when the GPU itself is the problem (Windows and
# Linux). Anything else, e.g. an unreadable input file, fails fast instead of
# silently costing a full CPU rerun.
_GPU_ERROR_MARKERS = (
    "cuda", "cublas", "cudnn", "is not found or cannot be loaded",
    "out of memory", "compute type", "unsupported device",
)
# GPU problems that will not go away within this process. A transient CUDA
# out-of-memory must stay retryable for the long-lived API process.
_PERMANENT_GPU_MARKERS = (
    "is not found or cannot be loaded", "unsupported device",
    "no cuda-capable device", "driver version is insufficient", "compute type",
)
# Of those, the ones that survive a settings change are written to the
# marker file; a bad WHISPER_COMPUTE must not pin later jobs to the CPU.
_PERSISTENT_GPU_MARKERS = (
    "is not found or cannot be loaded", "unsupported device",
    "no cuda-capable device", "driver version is insufficient",
)
_DOWNLOAD_SIZE = {
    "large-v3-turbo": "~1.6 GB", "turbo": "~1.6 GB", "small": "~0.5 GB",
    "medium": "~1.5 GB", "large-v3": "~3 GB",
}
LOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
# Every job is its own process, so a permanent GPU failure is also written to
# a marker file; later jobs skip the doomed GPU attempt until it expires.
GPU_DISABLED_MARKER = ".whisper-gpu-disabled.json"
GPU_DISABLED_TTL_SECONDS = 6 * 3600
# Files a complete CTranslate2 Whisper model folder always has.
_MODEL_FILES = ("model.bin", "config.json")
# Lock errors that mean "someone else holds the slot". Any other errno (a file
# system without byte-range locks) must not look like a busy GPU.
_BUSY_ERRNOS = {
    errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
    getattr(errno, "EDEADLK", -1), getattr(errno, "EDEADLOCK", -1),
}

_gpu_disabled_reason = None   # permanent GPU failure seen in this process
_announced_disabled = None    # last "GPU is off" reason printed in this process
LAST_RUN = {}                 # device/model/fell_back of the last run (logs, calibration)


class GpuTranscriptionError(Exception):
    """The GPU attempt failed in a way a CPU run can recover from."""


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _env_number(name, default, cast):
    raw = _env(name)
    if not raw:
        return default
    try:
        value = cast(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("not finite")
        return value
    except (ValueError, OverflowError):
        print(f"⚠️ {name}={raw!r} is not a usable number, using {default}")
        return default


def _ctranslate2_version():
    try:
        import ctranslate2
        return str(getattr(ctranslate2, "__version__", ""))
    except Exception:
        return ""


def _marker_path():
    return os.path.join(LOCK_DIR, GPU_DISABLED_MARKER)


def _persisted_gpu_failure():
    """Reason from a fresh marker of an earlier job, else None.

    A marker older than GPU_DISABLED_TTL_SECONDS or written by another
    CTranslate2 version is ignored, so a fixed driver or cuBLAS gets probed
    again. Delete the file to retry at once."""
    try:
        with open(_marker_path(), encoding="utf-8") as handle:
            marker = json.load(handle)
        age = time.time() - float(marker.get("at", 0))
        if 0 <= age < GPU_DISABLED_TTL_SECONDS and marker.get("ctranslate2") == _ctranslate2_version():
            return str(marker.get("reason") or "GPU disabled by an earlier job")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _persist_gpu_failure(reason):
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        with open(_marker_path(), "w", encoding="utf-8") as handle:
            json.dump({"reason": reason, "ctranslate2": _ctranslate2_version(), "at": time.time()}, handle)
    except OSError:
        pass


def _cuda_device_count():
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:          # ImportError in CI, broken driver or runtime
        return 0


def _supported_compute_types(device):
    try:
        import ctranslate2
        return set(ctranslate2.get_supported_compute_types(device))
    except Exception:
        return set()


def resolve_whisper_config():
    """Resolved config: model_size, device, compute_type, cpu_model, requested_device."""
    requested = _env("WHISPER_DEVICE", "auto").lower()
    requested = {"gpu": "cuda"}.get(requested, requested)
    if requested not in ("auto", "cuda", "cpu"):
        print(f"⚠️ WHISPER_DEVICE={requested!r} is not auto/cuda/cpu, using auto")
        requested = "auto"
    device = requested if requested != "auto" else (
        "cuda" if _cuda_device_count() > 0 else "cpu"
    )
    # The GPU-failure fallback always takes a CPU-sized model. A plain CPU run
    # prefers WHISPER_CPU_MODEL; a WHISPER_MODEL from an older .env (where it
    # was the CPU model) still applies when no CPU model is set.
    fallback_model = _env("WHISPER_CPU_MODEL") or DEFAULT_CPU_MODEL
    base = {"cpu_model": fallback_model, "requested_device": requested}
    if device == "cuda":
        disabled = _gpu_disabled_reason or _persisted_gpu_failure()
        if disabled:
            global _announced_disabled
            if disabled != _announced_disabled:
                _announced_disabled = disabled
                print(f"ℹ️ GPU transcription is off ({disabled}); using the CPU. "
                      f"Delete output/{GPU_DISABLED_MARKER} to retry the GPU now.")
            return cpu_config(base)
        model = _env("WHISPER_MODEL") or DEFAULT_GPU_MODEL
    else:
        model = _env("WHISPER_CPU_MODEL") or _env("WHISPER_MODEL") or DEFAULT_CPU_MODEL
    compute = _env("WHISPER_COMPUTE")
    supported = _supported_compute_types(device)
    if device == "cuda":
        if not compute:
            compute = next((c for c in GPU_COMPUTE_PREFERENCE if c in supported), "float16")
    elif not compute or (supported and compute not in supported):
        if compute:
            print(f"⚠️ WHISPER_COMPUTE={compute} is not supported on CPU, using int8")
        compute = CPU_COMPUTE_TYPE
    return {**base, "model_size": model, "device": device, "compute_type": compute}


def cpu_config(cfg):
    return {**cfg, "model_size": cfg["cpu_model"], "device": "cpu", "compute_type": CPU_COMPUTE_TYPE}


def describe_error(exc):
    return f"{type(exc).__name__}: {exc}".strip()


def is_gpu_error(exc):
    text = describe_error(exc).lower()
    return any(marker in text for marker in _GPU_ERROR_MARKERS)


def _note_gpu_failure(exc):
    global _gpu_disabled_reason
    text = describe_error(exc).lower()
    if any(marker in text for marker in _PERMANENT_GPU_MARKERS):
        _gpu_disabled_reason = describe_error(exc)
        if any(marker in text for marker in _PERSISTENT_GPU_MARKERS):
            _persist_gpu_failure(_gpu_disabled_reason)


def _batch_size():
    try:
        return max(0, int(_env("WHISPER_BATCH_SIZE", "0")))
    except ValueError:
        return 0


def ensure_model(name, on_status=None, report=None, allow_download=True):
    """Local path of a CTranslate2 Whisper model; downloads it on first use.

    A cached snapshot counts only when it is complete: an interrupted first
    download leaves the folder without model.bin, and only the online call
    resumes it. report["downloaded"] is set when the network was used."""
    if os.path.isdir(name):
        return name
    from faster_whisper.utils import download_model
    try:
        path = download_model(name, local_files_only=True)
        if all(os.path.isfile(os.path.join(path, file)) for file in _MODEL_FILES):
            return path
    except Exception:
        pass
    if not allow_download:
        raise FileNotFoundError(f"Whisper model {name} is not cached and downloads are off for this run")
    if report is not None:
        report["downloaded"] = True
    if on_status:
        on_status(
            f"Downloading Whisper model {name} "
            f"({_DOWNLOAD_SIZE.get(name, 'large')}, first run only)..."
        )
    return download_model(name)


def _try_lock(descriptor):
    """True when locked, False when another holder has it. Other lock errors
    (no lock support on this file system) raise OSError."""
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS:
            return False
        raise


class gpu_slot:
    """`with gpu_slot("whisper") as acquired:` caps GPU work across processes.

    Jobs run as separate processes (MAX_CONCURRENT_JOBS), so a threading lock
    is not enough. Same advisory-lock pattern as main._job_stats_file_lock:
    the OS drops the lock when its holder dies, and lock files are never
    deleted. A second handle in the same process conflicts too, so API
    threads are covered.
    """

    def __init__(self, name, slots=None, wait_seconds=None, lock_dir=None, on_wait=None, poll=1.0):
        self.name = name
        self.slots = _env_number("WHISPER_GPU_SLOTS", 1, int) if slots is None else int(slots)
        self.wait_seconds = (
            _env_number("WHISPER_GPU_WAIT_SECONDS", 900.0, float) if wait_seconds is None else float(wait_seconds)
        )
        self.lock_dir = lock_dir or LOCK_DIR
        self.on_wait = on_wait
        self.poll = poll
        self._descriptor = None
        self.waited_seconds = 0.0

    def __enter__(self):
        if self.slots <= 0:
            return True
        try:
            os.makedirs(self.lock_dir, exist_ok=True)
        except OSError:
            return True                      # degrade to ungated
        started = time.monotonic()
        deadline = started + max(0.0, self.wait_seconds)
        announced = started
        while True:
            for index in range(self.slots):
                path = os.path.join(self.lock_dir, f".gpu-{self.name}-{index}.lock")
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_RDWR)
                except OSError:
                    return True
                try:
                    locked = _try_lock(descriptor)
                except OSError:
                    os.close(descriptor)
                    return True                  # no lock support: degrade to ungated
                if locked:
                    self._descriptor = descriptor
                    self.waited_seconds = time.monotonic() - started
                    return True
                os.close(descriptor)
            now = time.monotonic()
            if now >= deadline:
                self.waited_seconds = now - started
                return False
            if self.on_wait and now - announced >= 30:
                self.on_wait(int(deadline - now))
                announced = now
            time.sleep(self.poll)

    def __exit__(self, *_exc):
        if self._descriptor is not None:
            try:
                if os.name == "nt":
                    os.lseek(self._descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._descriptor)
            self._descriptor = None
        return False


def release_model(model):
    """Hand the VRAM back before rendering uses the GPU encoder.

    CTranslate2 frees weights and its allocator cache on unload_model(), not
    on garbage collection. Exception tracebacks keep the model object alive,
    so the explicit unload matters on the failure path too.
    """
    if model is None:
        return
    try:
        model.model.unload_model()
    except Exception as exc:
        print(f"⚠️ Whisper unload failed ({describe_error(exc)})")
    gc.collect()


def _attempt(media_path, params, cfg, on_segment, on_status, log):
    from faster_whisper import WhisperModel   # ImportError is not a GPU problem
    on_gpu = cfg["device"] == "cuda"
    log(f"🎙️ Whisper {cfg['model_size']} on {cfg['device']} ({cfg['compute_type']})")
    if on_status:
        on_status(f"Transcribing on {'GPU' if on_gpu else 'CPU'} with Whisper {cfg['model_size']}")
    model = None
    try:
        try:
            model = WhisperModel(
                cfg.get("model_path") or cfg["model_size"],
                device=cfg["device"],
                compute_type=cfg["compute_type"],
            )
        except Exception as exc:              # load stage: always worth a CPU retry
            if not on_gpu:
                raise
            _note_gpu_failure(exc)
            raise GpuTranscriptionError(describe_error(exc)) from exc
        try:
            runner, call_params = model, dict(params)
            batch = _batch_size() if on_gpu else 0
            if batch > 0:
                from faster_whisper import BatchedInferencePipeline
                runner = BatchedInferencePipeline(model)
                call_params.update(batch_size=batch, without_timestamps=False)
            # faster-whisper runs the encoder here (language detection) and
            # decodes lazily while the generator is iterated, so GPU errors
            # can surface at both places.
            segments, info = runner.transcribe(media_path, **call_params)
            materialized = []
            for segment in segments:
                materialized.append(segment)
                if on_segment:
                    on_segment(segment, info)
        except Exception as exc:
            if not (on_gpu and is_gpu_error(exc)):
                raise
            _note_gpu_failure(exc)
            raise GpuTranscriptionError(describe_error(exc)) from exc
        LAST_RUN.clear()
        LAST_RUN.update(
            device=cfg["device"],
            model_size=cfg["model_size"],
            compute_type=cfg["compute_type"],
            fell_back=False,
        )
        return materialized, info, cfg
    finally:
        release_model(model)
        model = None


def run_whisper(media_path, params, *, on_segment=None, on_restart=None, on_status=None,
                on_wait=None, allow_gpu_download=True, log=print):
    """Transcribe media_path; returns (segments, info, cfg_used).

    A GPU failure at load time, in transcribe() or while iterating discards
    the partial result, frees the VRAM and restarts once from the beginning
    on the CPU with WHISPER_CPU_MODEL. on_wait(seconds_left) is called while
    another job holds the GPU (default: an on_status message).
    LAST_RUN records gpu_wait_seconds and model_downloaded: that time is not
    transcription speed. allow_gpu_download=False sends a GPU run whose model
    is not cached straight to the CPU (the caller's download already failed).
    """
    cfg = resolve_whisper_config()
    download = {}
    if cfg["device"] != "cuda":
        try:
            cfg = {**cfg, "model_path": ensure_model(cfg["model_size"], on_status, report=download)}
        except Exception:
            pass                                   # WhisperModel reports the real error
        result = _attempt(media_path, params, cfg, on_segment, on_status, log)
        LAST_RUN.update(gpu_wait_seconds=0.0, model_downloaded=bool(download.get("downloaded")))
        return result
    reason = None
    model_path = None
    try:
        model_path = ensure_model(                  # outside the GPU slot
            cfg["model_size"], on_status, report=download, allow_download=allow_gpu_download,
        )
    except Exception as exc:
        reason = f"model download failed ({describe_error(exc)})"
    slot = None
    if reason is None:
        def waiting(seconds_left):
            if on_wait:
                on_wait(seconds_left)
            elif on_status:
                on_status(
                    "Waiting for the GPU: another transcription is running "
                    f"(CPU fallback in {seconds_left}s)"
                )

        slot = gpu_slot("whisper", on_wait=waiting)
        with slot as acquired:
            if acquired:
                try:
                    result = _attempt(
                        media_path, params, {**cfg, "model_path": model_path},
                        on_segment, on_status, log,
                    )
                    LAST_RUN.update(gpu_wait_seconds=slot.waited_seconds,
                                    model_downloaded=bool(download.get("downloaded")))
                    return result
                except GpuTranscriptionError as failure:
                    reason = str(failure)
            else:
                reason = "GPU busy: no free transcription slot"
    fallback = cpu_config(cfg)
    log(
        f"⚠️ Whisper on GPU failed ({reason}), restarting on CPU with "
        f"{fallback['model_size']} ({CPU_COMPUTE_TYPE})"
    )
    if on_restart:
        on_restart(reason, fallback)
    result = _attempt(media_path, params, fallback, on_segment, on_status, log)
    LAST_RUN.update(fell_back=True, fallback_reason=reason,
                    gpu_wait_seconds=slot.waited_seconds if slot else 0.0,
                    model_downloaded=bool(download.get("downloaded")))
    return result
