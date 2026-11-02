"""faster-whisper runtime shared by every transcription call site.

Picks the NVIDIA GPU when CTranslate2 sees one (large-v3-turbo, float16) and
falls back to the CPU (small, int8) when the GPU cannot be used. The policy is
adapted from upstream openshorts transcribe_backends.py (MIT), with Windows
error texts, a CPU-sized fallback model and explicit VRAM release.

Stdlib-only at import time: CI has neither ctranslate2 nor faster-whisper.
"""
import gc
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
_DOWNLOAD_SIZE = {
    "large-v3-turbo": "~1.6 GB", "turbo": "~1.6 GB", "small": "~0.5 GB",
    "medium": "~1.5 GB", "large-v3": "~3 GB",
}
LOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

_gpu_disabled_reason = None   # permanent GPU failure seen in this process
LAST_RUN = {}                 # device/model/fell_back of the last run (logs, calibration)


class GpuTranscriptionError(Exception):
    """The GPU attempt failed in a way a CPU run can recover from."""


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


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
    base = {"cpu_model": _env("WHISPER_CPU_MODEL", DEFAULT_CPU_MODEL), "requested_device": requested}
    if device == "cuda" and _gpu_disabled_reason:
        return cpu_config(base)
    model = _env("WHISPER_MODEL") or (DEFAULT_GPU_MODEL if device == "cuda" else DEFAULT_CPU_MODEL)
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


def _batch_size():
    try:
        return max(0, int(_env("WHISPER_BATCH_SIZE", "0")))
    except ValueError:
        return 0


def ensure_model(name, on_status=None):
    """Local path of a CTranslate2 Whisper model; downloads it on first use."""
    if os.path.isdir(name):
        return name
    from faster_whisper.utils import download_model
    try:
        return download_model(name, local_files_only=True)
    except Exception:
        pass
    if on_status:
        on_status(
            f"Downloading Whisper model {name} "
            f"({_DOWNLOAD_SIZE.get(name, 'large')}, first run only)..."
        )
    return download_model(name)


def _try_lock(descriptor):
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


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
        self.slots = int(_env("WHISPER_GPU_SLOTS", "1")) if slots is None else int(slots)
        self.wait_seconds = (
            float(_env("WHISPER_GPU_WAIT_SECONDS", "900")) if wait_seconds is None else float(wait_seconds)
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
                if _try_lock(descriptor):
                    self._descriptor = descriptor
                    self.waited_seconds = time.monotonic() - started
                    return True
                os.close(descriptor)
            now = time.monotonic()
            if now >= deadline:
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


def run_whisper(media_path, params, *, on_segment=None, on_restart=None, on_status=None, log=print):
    """Transcribe media_path; returns (segments, info, cfg_used).

    A GPU failure at load time, in transcribe() or while iterating discards
    the partial result, frees the VRAM and restarts once from the beginning
    on the CPU with WHISPER_CPU_MODEL.
    """
    cfg = resolve_whisper_config()
    if cfg["device"] != "cuda":
        return _attempt(media_path, params, cfg, on_segment, on_status, log)
    reason = None
    model_path = None
    try:
        model_path = ensure_model(cfg["model_size"], on_status)   # outside the GPU slot
    except Exception as exc:
        reason = f"model download failed ({describe_error(exc)})"
    if reason is None:
        def waiting(seconds_left):
            if on_status:
                on_status(
                    "Waiting for the GPU: another transcription is running "
                    f"(CPU fallback in {seconds_left}s)"
                )

        with gpu_slot("whisper", on_wait=waiting) as acquired:
            if acquired:
                try:
                    return _attempt(
                        media_path, params, {**cfg, "model_path": model_path},
                        on_segment, on_status, log,
                    )
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
    LAST_RUN.update(fell_back=True, fallback_reason=reason)
    return result
