import importlib
import sys
import types

import pytest

import whisper_runtime


class _FakeCt2Model:
    def __init__(self):
        self.unloaded = False

    def unload_model(self):
        self.unloaded = True


class _Backend:
    """Fake ctranslate2 + faster_whisper with per-device failure injection."""

    def __init__(self):
        self.cuda_devices = 1
        self.cuda_compute = {"float16", "int8_float16", "int8", "float32"}
        self.load_error = {}          # device -> exception raised by WhisperModel()
        self.transcribe_error = {}    # device -> exception raised by transcribe()
        self.iter_error = {}          # device -> (yield count, exception)
        self.segments = {"cuda": 2, "cpu": 3}
        self.models = []
        self.transcribe_calls = []
        self.downloads = []
        self.download_error = None
        self.local_cache = None       # path download_model(local_files_only=True) returns
        self.batched = []

    def install(self, monkeypatch):
        backend = self

        ct2 = types.ModuleType("ctranslate2")
        ct2.get_cuda_device_count = lambda: backend.cuda_devices
        ct2.get_supported_compute_types = lambda device: (
            backend.cuda_compute if device == "cuda" else {"int8", "float32"}
        )

        class WhisperModel:
            def __init__(self, source, device="cpu", compute_type="int8"):
                self.source, self.device, self.compute_type = source, device, compute_type
                self.model = _FakeCt2Model()
                backend.models.append(self)
                if device in backend.load_error:
                    raise backend.load_error[device]

            def transcribe(self, media_path, **params):
                backend.transcribe_calls.append((self.device, dict(params)))
                if self.device in backend.transcribe_error:
                    raise backend.transcribe_error[self.device]
                info = types.SimpleNamespace(language="de", language_probability=0.99)
                return backend._generator(self.device), info

        class BatchedInferencePipeline:
            def __init__(self, model):
                self.model = model

            def transcribe(self, media_path, **params):
                backend.batched.append(dict(params))
                return self.model.transcribe(media_path, **params)

        fw = types.ModuleType("faster_whisper")
        fw.WhisperModel = WhisperModel
        fw.BatchedInferencePipeline = BatchedInferencePipeline
        utils = types.ModuleType("faster_whisper.utils")

        def download_model(name, local_files_only=False):
            backend.downloads.append((name, local_files_only))
            if local_files_only:
                if backend.local_cache:
                    return backend.local_cache
                raise FileNotFoundError("not cached")
            if backend.download_error:
                raise backend.download_error
            return f"/models/{name}"

        utils.download_model = download_model
        fw.utils = utils
        monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
        monkeypatch.setitem(sys.modules, "faster_whisper", fw)
        monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
        return self

    def _generator(self, device):
        failing = self.iter_error.get(device)
        for index in range(self.segments[device]):
            if failing and index == failing[0]:
                raise failing[1]
            yield types.SimpleNamespace(device=device, index=index, start=float(index), end=float(index + 1))


@pytest.fixture
def backend(monkeypatch, tmp_path):
    for name in (
        "WHISPER_DEVICE", "WHISPER_MODEL", "WHISPER_COMPUTE", "WHISPER_CPU_MODEL",
        "WHISPER_GPU_SLOTS", "WHISPER_GPU_WAIT_SECONDS", "WHISPER_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(whisper_runtime, "_gpu_disabled_reason", None)
    monkeypatch.setattr(whisper_runtime, "LAST_RUN", {})
    monkeypatch.setattr(whisper_runtime, "LOCK_DIR", str(tmp_path))
    return _Backend().install(monkeypatch)


def _run(**kwargs):
    events = []
    result = whisper_runtime.run_whisper(
        "audio.mp4",
        {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False, "word_timestamps": True},
        on_segment=lambda segment, _info: events.append(("segment", segment.device)),
        on_restart=lambda reason, cfg: events.append(("restart", reason, cfg["model_size"])),
        log=lambda _message: None,
        **kwargs,
    )
    return result, events


def test_auto_picks_cuda_turbo_float16_when_ctranslate2_sees_a_gpu(backend):
    cfg = whisper_runtime.resolve_whisper_config()
    assert (cfg["model_size"], cfg["device"], cfg["compute_type"]) == ("large-v3-turbo", "cuda", "float16")


def test_auto_is_cpu_small_int8_without_ctranslate2(backend, monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    cfg = whisper_runtime.resolve_whisper_config()
    assert (cfg["model_size"], cfg["device"], cfg["compute_type"]) == ("small", "cpu", "int8")


def test_auto_is_cpu_when_cuda_probe_raises(backend, monkeypatch):
    def broken():
        raise RuntimeError("CUDA driver version is insufficient")

    monkeypatch.setattr(sys.modules["ctranslate2"], "get_cuda_device_count", broken)
    assert whisper_runtime.resolve_whisper_config()["device"] == "cpu"


def test_env_overrides_device_model_and_compute(backend, monkeypatch):
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    assert whisper_runtime.resolve_whisper_config()["model_size"] == "small"
    monkeypatch.setenv("WHISPER_DEVICE", "gpu")
    monkeypatch.setenv("WHISPER_MODEL", "medium")
    monkeypatch.setenv("WHISPER_COMPUTE", "int8_float16")
    cfg = whisper_runtime.resolve_whisper_config()
    assert (cfg["device"], cfg["model_size"], cfg["compute_type"]) == ("cuda", "medium", "int8_float16")
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("WHISPER_COMPUTE", "float16")
    assert whisper_runtime.resolve_whisper_config()["compute_type"] == "int8"
    monkeypatch.setenv("WHISPER_DEVICE", "toaster")
    assert whisper_runtime.resolve_whisper_config()["requested_device"] == "auto"


def test_cuda_compute_follows_supported_types(backend):
    backend.cuda_compute = {"int8", "float32"}
    assert whisper_runtime.resolve_whisper_config()["compute_type"] == "int8"


@pytest.mark.parametrize("text", [
    "RuntimeError: Library cublas64_12.dll is not found or cannot be loaded",
    "Library libcublas.so.12 is not found or cannot be loaded",
    "CUDA failed with error out of memory",
    "cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED",
    "cuDNN failed with status CUDNN_STATUS_EXECUTION_FAILED",
    "unsupported device cuda",
    "Requested float16 compute type, but the target device or backend do not support efficient float16 computation",
])
def test_is_gpu_error_matches_windows_and_linux_texts(text):
    assert whisper_runtime.is_gpu_error(RuntimeError(text))


@pytest.mark.parametrize("exc", [
    RuntimeError("Invalid data found when processing input"),
    IndexError("tuple index out of range"),
    FileNotFoundError("audio.mp4"),
])
def test_is_gpu_error_ignores_input_problems(exc):
    assert not whisper_runtime.is_gpu_error(exc)


def test_load_oom_on_cuda_restarts_on_cpu_with_cpu_model(backend):
    backend.load_error["cuda"] = RuntimeError("CUDA failed with error out of memory")
    (segments, _info, cfg), events = _run()
    assert [(m.device, m.source, m.compute_type) for m in backend.models] == [
        ("cuda", "/models/large-v3-turbo", "float16"), ("cpu", "small", "int8"),
    ]
    assert [e for e in events if e[0] == "restart"] == [
        ("restart", "RuntimeError: CUDA failed with error out of memory", "small"),
    ]
    assert cfg["device"] == "cpu" and len(segments) == 3
    assert whisper_runtime.LAST_RUN["fell_back"] is True
    assert whisper_runtime._gpu_disabled_reason is None   # OOM is transient


def test_model_download_failure_on_cuda_runs_on_cpu(backend):
    backend.download_error = OSError("offline")
    (_segments, _info, cfg), events = _run()
    assert cfg["device"] == "cpu"
    assert [m.device for m in backend.models] == ["cpu"]
    assert events[0][0] == "restart" and "model download failed" in events[0][1]


def test_error_in_transcribe_call_falls_back(backend):
    backend.transcribe_error["cuda"] = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    (segments, _info, cfg), _events = _run()
    assert cfg["device"] == "cpu" and len(segments) == 3


def test_mid_iteration_gpu_error_discards_partial_segments_and_restarts(backend):
    backend.segments["cuda"] = 5
    backend.iter_error["cuda"] = (2, RuntimeError("Library cublas64_12.dll is not found or cannot be loaded"))
    (segments, _info, _cfg), events = _run()
    assert [s.device for s in segments] == ["cpu", "cpu", "cpu"]
    assert [e[0] if e[0] != "segment" else e[1] for e in events] == [
        "cuda", "cuda", "restart", "cpu", "cpu", "cpu",
    ]
    assert backend.models[0].model.unloaded is True
    assert whisper_runtime._gpu_disabled_reason   # missing library is permanent


def test_permanent_failure_pins_cpu_but_oom_does_not(backend, tmp_path):
    backend.load_error["cuda"] = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    _run()
    backend.models.clear()
    _run()
    assert [m.device for m in backend.models] == ["cpu"]

    # The next job is a new process: the marker file still pins the CPU.
    whisper_runtime._gpu_disabled_reason = None
    backend.models.clear()
    _run()
    assert [m.device for m in backend.models] == ["cpu"]

    (tmp_path / whisper_runtime.GPU_DISABLED_MARKER).unlink()
    whisper_runtime._gpu_disabled_reason = None
    backend.load_error["cuda"] = RuntimeError("CUDA failed with error out of memory")
    _run()
    backend.models.clear()
    del backend.load_error["cuda"]
    _run()
    assert [m.device for m in backend.models] == ["cuda"]


def test_non_gpu_error_on_cuda_is_not_retried(backend):
    backend.iter_error["cuda"] = (1, RuntimeError("Invalid data found when processing input"))
    with pytest.raises(RuntimeError, match="Invalid data"):
        _run()
    assert len(backend.models) == 1 and backend.models[0].model.unloaded is True


def test_cpu_errors_are_never_retried(backend, monkeypatch):
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    backend.transcribe_error["cpu"] = RuntimeError("CUDA failed with error out of memory")
    with pytest.raises(RuntimeError):
        _run()
    assert len(backend.models) == 1


def test_model_is_unloaded_after_success(backend):
    (_segments, _info, cfg), _events = _run()
    assert cfg["device"] == "cuda"
    assert backend.models[0].model.unloaded is True
    last_run = dict(whisper_runtime.LAST_RUN)
    assert last_run.pop("gpu_wait_seconds") < 1.0
    assert last_run == {
        "device": "cuda", "model_size": "large-v3-turbo", "compute_type": "float16", "fell_back": False,
        "model_downloaded": True,
    }


def test_params_reach_model_unchanged_on_both_attempts(backend):
    backend.iter_error["cuda"] = (1, RuntimeError("CUDA failed with error out of memory"))
    params = {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False, "word_timestamps": True}
    snapshot = dict(params)
    whisper_runtime.run_whisper("audio.mp4", params, log=lambda _m: None)
    assert [call[1] for call in backend.transcribe_calls] == [snapshot, snapshot]
    assert params == snapshot


def test_batched_pipeline_is_opt_in_and_gpu_only(backend, monkeypatch):
    _run()
    assert backend.batched == []
    monkeypatch.setenv("WHISPER_BATCH_SIZE", "8")
    _run()
    assert backend.batched[-1]["batch_size"] == 8
    assert backend.batched[-1]["without_timestamps"] is False
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    count = len(backend.batched)
    _run()
    assert len(backend.batched) == count


def test_gpu_slot_is_exclusive_and_released(tmp_path):
    first = whisper_runtime.gpu_slot("test", slots=1, wait_seconds=0, lock_dir=str(tmp_path))
    assert first.__enter__() is True
    try:
        with whisper_runtime.gpu_slot("test", slots=1, wait_seconds=0, lock_dir=str(tmp_path)) as acquired:
            assert acquired is False
    finally:
        first.__exit__(None, None, None)
    with whisper_runtime.gpu_slot("test", slots=1, wait_seconds=0, lock_dir=str(tmp_path)) as acquired:
        assert acquired is True


def test_busy_gpu_slot_times_out_to_cpu(backend, monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_GPU_WAIT_SECONDS", "0")
    holder = whisper_runtime.gpu_slot("whisper", slots=1, wait_seconds=0, lock_dir=str(tmp_path))
    assert holder.__enter__() is True
    try:
        (_segments, _info, cfg), events = _run()
    finally:
        holder.__exit__(None, None, None)
    assert cfg["device"] == "cpu"
    assert [m.device for m in backend.models] == ["cpu"]
    assert events[0][0] == "restart" and events[0][1].startswith("GPU busy")


def test_module_imports_without_heavy_deps(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    for name in ("WHISPER_DEVICE", "WHISPER_MODEL", "WHISPER_COMPUTE"):
        monkeypatch.delenv(name, raising=False)
    reloaded = importlib.reload(whisper_runtime)
    try:
        monkeypatch.setattr(reloaded, "_gpu_disabled_reason", None)
        assert reloaded.resolve_whisper_config()["device"] == "cpu"
    finally:
        importlib.reload(whisper_runtime)


def test_partial_model_cache_is_downloaded_again(backend, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}")          # model.bin never arrived
    backend.local_cache = str(snapshot)
    report = {}
    assert whisper_runtime.ensure_model("large-v3-turbo", report=report) == "/models/large-v3-turbo"
    assert backend.downloads == [("large-v3-turbo", True), ("large-v3-turbo", False)]
    assert report == {"downloaded": True}


def test_complete_model_cache_is_used_offline(backend, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for name in ("config.json", "model.bin"):
        (snapshot / name).write_text("x")
    backend.local_cache = str(snapshot)
    report = {}
    assert whisper_runtime.ensure_model("large-v3-turbo", report=report) == str(snapshot)
    assert backend.downloads == [("large-v3-turbo", True)] and report == {}


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"WHISPER_CPU_MODEL": "base"}, "base"),
        ({"WHISPER_MODEL": "large-v3", "WHISPER_CPU_MODEL": "small"}, "small"),
        ({"WHISPER_MODEL": "medium"}, "medium"),                 # older .env: WHISPER_MODEL was the CPU model
        ({}, "small"),
    ],
)
def test_cpu_runs_use_the_cpu_model(backend, monkeypatch, env, expected):
    backend.cuda_devices = 0
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg = whisper_runtime.resolve_whisper_config()
    assert (cfg["device"], cfg["model_size"]) == ("cpu", expected)


def test_gpu_fallback_never_takes_the_gpu_sized_model(backend, monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v3")
    backend.load_error["cuda"] = RuntimeError("CUDA failed with error out of memory")
    (_segments, _info, cfg), _events = _run()
    assert (cfg["device"], cfg["model_size"]) == ("cpu", "small")


def test_stale_or_foreign_gpu_marker_is_ignored(backend, tmp_path, monkeypatch):
    import json
    import time

    marker = tmp_path / whisper_runtime.GPU_DISABLED_MARKER
    marker.write_text(json.dumps({"reason": "old", "ctranslate2": "", "at": time.time() - 7 * 3600}))
    assert whisper_runtime.resolve_whisper_config()["device"] == "cuda"
    marker.write_text(json.dumps({"reason": "other build", "ctranslate2": "0.0.1", "at": time.time()}))
    assert whisper_runtime.resolve_whisper_config()["device"] == "cuda"
    marker.write_text("not json")
    assert whisper_runtime.resolve_whisper_config()["device"] == "cuda"


def test_lock_without_file_system_support_runs_ungated(monkeypatch, tmp_path):
    import errno

    def no_lock_support(_descriptor):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(whisper_runtime, "_try_lock", no_lock_support)
    with whisper_runtime.gpu_slot("t", slots=1, wait_seconds=30, lock_dir=str(tmp_path), poll=0.01) as acquired:
        assert acquired is True


@pytest.mark.parametrize("code,busy", [("EACCES", True), ("EAGAIN", True), ("EINVAL", False), ("ENOLCK", False)])
def test_try_lock_only_reports_busy_for_contention(monkeypatch, code, busy):
    import errno
    import os

    def failing(*_args):
        raise OSError(getattr(errno, code), code)

    if os.name == "nt":
        monkeypatch.setattr(whisper_runtime.msvcrt, "locking", failing)
    else:
        monkeypatch.setattr(whisper_runtime.fcntl, "flock", failing)
    if busy:
        assert whisper_runtime._try_lock(0) is False
    else:
        with pytest.raises(OSError):
            whisper_runtime._try_lock(0)


def test_bad_slot_settings_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("WHISPER_GPU_SLOTS", "1.5")
    monkeypatch.setenv("WHISPER_GPU_WAIT_SECONDS", "15m")
    slot = whisper_runtime.gpu_slot("t")
    assert (slot.slots, slot.wait_seconds) == (1, 900.0)


def test_busy_slot_falls_back_and_records_the_wait(backend, monkeypatch, tmp_path):
    waits = []
    holder = whisper_runtime.gpu_slot("whisper", slots=1, wait_seconds=1, lock_dir=str(tmp_path))
    monkeypatch.setenv("WHISPER_GPU_WAIT_SECONDS", "0")
    with holder:
        (_segments, _info, cfg), _events = _run(on_wait=waits.append)
    assert cfg["device"] == "cpu"
    assert whisper_runtime.LAST_RUN["fell_back"] is True
    assert whisper_runtime.LAST_RUN["gpu_wait_seconds"] >= 0.0


def test_full_slot_timeout_records_the_wait(tmp_path):
    holder = whisper_runtime.gpu_slot("t", slots=1, wait_seconds=1, lock_dir=str(tmp_path))
    waiter = whisper_runtime.gpu_slot("t", slots=1, wait_seconds=0.2, lock_dir=str(tmp_path), poll=0.05)
    with holder:
        with waiter as acquired:
            assert acquired is False
    assert waiter.waited_seconds >= 0.2


@pytest.mark.parametrize("raw", ["inf", "nan", "-inf", "1e400"])
def test_infinite_wait_setting_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("WHISPER_GPU_WAIT_SECONDS", raw)
    assert whisper_runtime.gpu_slot("t").wait_seconds == 900.0


def test_bad_compute_type_is_not_persisted_for_later_jobs(backend, tmp_path):
    backend.load_error["cuda"] = RuntimeError("requested float16 compute type, but the device does not support it")
    _run()
    assert whisper_runtime._gpu_disabled_reason            # this process stays on the CPU
    assert not (tmp_path / whisper_runtime.GPU_DISABLED_MARKER).exists()


def test_failed_prefetch_sends_the_gpu_run_straight_to_cpu(backend):
    (_segments, _info, cfg), events = _run(allow_gpu_download=False)
    assert cfg["device"] == "cpu"
    assert ("large-v3-turbo", False) not in backend.downloads    # no second download attempt
    assert events[0][0] == "restart" and "download" in events[0][1]


def test_cpu_run_reports_its_model_download(backend):
    backend.cuda_devices = 0
    (_segments, _info, cfg), _events = _run()
    assert cfg["model_path"] == "/models/small"
    assert whisper_runtime.LAST_RUN["model_downloaded"] is True
