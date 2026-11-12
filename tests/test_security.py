"""Harmless regression cases: synthetic data, temporary files, no paid APIs."""
import asyncio
import io
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from pydantic import ValidationError

import app
import saasshorts


@pytest.fixture
def isolated_app(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    output = tmp_path / "output"
    thumbs = output / "thumbnails"
    uploads.mkdir()
    thumbs.mkdir(parents=True)
    for name, value in (("UPLOAD_DIR", uploads), ("OUTPUT_DIR", output), ("THUMBNAILS_DIR", thumbs)):
        monkeypatch.setattr(app, name, str(value))
    for name in ("jobs", "thumbnail_sessions", "publish_jobs", "saas_jobs", "job_state_locks", "clip_operation_locks"):
        monkeypatch.setattr(app, name, {})
    for name in ("_persist_thumbnail_session", "_persist_publish_job", "_persist_saas_job", "_persist_job_state"):
        monkeypatch.setattr(app, name, lambda *args: None)
    monkeypatch.setattr(app, "job_queue", asyncio.Queue())
    monkeypatch.delenv("OPENSHORTS_API_TOKEN", raising=False)
    monkeypatch.delenv("OPENSHORTS_ALLOWED_ORIGINS", raising=False)
    return uploads, output, thumbs


def test_publish_rejects_thumbnail_path_escape(monkeypatch, isolated_app, tmp_path):
    _, _, _ = isolated_app
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video")
    marker = tmp_path / "marker.txt"
    marker.write_text("harmless audit marker", encoding="utf-8")
    app.thumbnail_sessions["safe-session"] = {"video_path": str(video)}
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.thumbnail_publish(tasks, "safe-session", "title", "desc", "../../marker.txt", "fake-key", "fake-user"))
    assert exc.value.status_code == 400
    assert tasks.tasks == []


def test_thumbnail_rejects_traversal_session(monkeypatch, isolated_app):
    monkeypatch.setattr(app, "generate_thumbnail", lambda *args: ["synthetic.jpg"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.thumbnail_generate(SimpleNamespace(), "../../../escaped", "title", "", 1, None, None, "fake-key"))
    assert exc.value.status_code == 400


def test_status_rejects_path_job_id(isolated_app):
    _, output, _ = isolated_app
    outside = output.parent / "outside"
    outside.mkdir()
    (outside / "job_state.json").write_text('{"job_id":"outside","status":"completed","input_filename":"harmless marker"}', encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.get_status("../outside"))
    assert exc.value.status_code == 400


def test_scraper_rejects_redirect_to_loopback(monkeypatch):
    pytest.importorskip("bs4")
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    requests = []
    real_client = httpx.Client

    def respond(request):
        requests.append(str(request.url))
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, text="<title>LOCAL_TEST_MARKER</title>")
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8123/private"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ValueError):
        saasshorts.scrape_website("https://public.example")
    assert all("127.0.0.1" not in url for url in requests)


def test_process_rejects_non_youtube_url_before_probe(monkeypatch, isolated_app):
    calls = []
    async def json_body():
        return {"url": "http://127.0.0.1:8123/private"}
    async def probe(url):
        calls.append(url)
        return {}
    monkeypatch.setattr(app, "_probe_youtube_quality", probe)
    request = SimpleNamespace(headers={"X-Gemini-Key": "fake-key", "content-type": "application/json"}, json=json_body)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app._process_endpoint_body(request, None, None, None, None, None, None))
    assert exc.value.status_code == 400
    assert calls == []


@pytest.mark.parametrize("count", [0, -1, 1000])
def test_actor_options_are_bounded(count):
    with pytest.raises(ValidationError):
        app.SaaSActorRequest(actor_description="synthetic", num_options=count)


def test_video_page_encodes_untrusted_metadata(monkeypatch):
    payload = '</p><img src=x onerror="window.__audit_marker=1">'
    monkeypatch.setattr(app, "list_video_gallery", lambda count: [{
        "video_id": "test", "title": "synthetic", "hashtags": [payload],
        "language": 'en" onload="window.__audit_marker=1',
        "product_url": "javascript:window.__audit_marker=1",
        "actor_url": 'https://example.test/a.jpg" onerror="window.__audit_marker=1',
    }])
    html = asyncio.run(app.video_html_page("test"))
    assert payload not in html
    assert '<html lang="en" onload=' not in html
    assert 'href="javascript:' not in html
    assert 'src="https://example.test/a.jpg" onerror=' not in html


def _http_get(path, *, client_host="127.0.0.1", headers=None):
    async def run():
        transport = httpx.ASGITransport(app=app.app, client=(client_host, 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            return await client.get(path, headers=headers)
    return asyncio.run(run())


def test_api_rejects_remote_peer(isolated_app):
    _, output, _ = isolated_app
    app.jobs["synthetic-job"] = app._build_job_state("synthetic-job", output_dir=str(output), status="completed", input_filename="private synthetic marker")
    response = _http_get("/api/status/synthetic-job", client_host="192.0.2.20")
    assert response.status_code == 403


def test_api_rejects_foreign_browser_origin(isolated_app):
    _, output, _ = isolated_app
    app.jobs["synthetic-job"] = app._build_job_state("synthetic-job", output_dir=str(output), status="completed", input_filename="private synthetic marker")
    response = _http_get("/api/status/synthetic-job", headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403


def test_private_metadata_is_not_served(monkeypatch, isolated_app):
    _, output, _ = isolated_app
    folder = output / "synthetic-job"
    folder.mkdir()
    (folder / "job_state.json").write_text('{"private_marker":true}', encoding="utf-8")
    for route in app.app.routes:
        if getattr(route, "path", None) == "/videos":
            monkeypatch.setattr(route.app, "directory", str(output))
            monkeypatch.setattr(route.app, "all_directories", [str(output)])
    response = _http_get("/videos/synthetic-job/job_state.json")
    assert response.status_code == 404
    assert "private_marker" not in response.text


def test_thumbnail_analyze_enforces_upload_limit(monkeypatch, isolated_app):
    monkeypatch.setattr(app, "MAX_FILE_SIZE_MB", 1)
    monkeypatch.setattr(app, "analyze_video_for_titles", lambda *args: {"titles": ["synthetic"]})
    upload = UploadFile(filename="synthetic.mp4", file=io.BytesIO(b"x" * (1024 * 1024 + 1)))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.thumbnail_analyze(SimpleNamespace(), upload, None, None, "fake-key"))
    assert exc.value.status_code == 413


@pytest.mark.parametrize("relative", ["../marker", "..\\marker", "/absolute", "C:\\marker", "C:marker", "//host/share", "dir/../../marker", "dir/../marker", "a\x00b"])
def test_storage_rejects_platform_path_escapes(tmp_path, relative):
    from security import confined_path
    with pytest.raises(ValueError):
        confined_path(tmp_path, relative)


def test_storage_rejects_symlink_escape(tmp_path):
    from security import confined_path
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires a Windows privilege")
    with pytest.raises(ValueError):
        confined_path(root, "link/marker")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "http://10.1.1.1/", "http://224.0.0.1/", "file:///marker", "ftp://public.example/", "https://user:pass@public.example/", "https://public.example:8443/", "http://[::ffff:127.0.0.1]/"])
def test_download_rejects_unsafe_destinations(url):
    from security import _public_target
    with pytest.raises(ValueError):
        _public_target(url)


def test_dns_answers_are_checked_and_connection_is_pinned(monkeypatch):
    import socket
    from security import public_text
    dns_calls = []
    def resolve(*args, **kwargs):
        dns_calls.append(args[0])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    real_client = httpx.Client
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"synthetic public page"))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    assert public_text("https://public.example/page") == "synthetic public page"
    assert dns_calls == ["public.example"]
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"


def test_mixed_public_private_dns_is_rejected(monkeypatch):
    import socket
    from security import _public_target
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", (ip, 443)) for ip in ("93.184.216.34", "127.0.0.1")])
    with pytest.raises(ValueError):
        _public_target("https://public.example/")


@pytest.mark.parametrize("url", ["file:///marker", "http://127.0.0.1/", "https://youtube.com.attacker.example/watch?v=abcdefghijk", "https://youtube.com@attacker.example/", "https://youtube.com\\@attacker.example/", "https://www.youtube.com:8443/"])
def test_youtube_rejects_host_and_protocol_bypasses(url):
    from security import validate_youtube_url
    with pytest.raises(ValueError):
        validate_youtube_url(url)


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=abcdefghijk", "https://youtu.be/abcdefghijk", "https://m.youtube.com/shorts/abcdefghijk"])
def test_youtube_keeps_supported_urls(url):
    from security import validate_youtube_url
    assert validate_youtube_url(url) == url


def test_upload_never_overwrites_existing_file(isolated_app, tmp_path):
    marker = tmp_path / "marker.mp4"
    marker.write_bytes(b"original")
    upload = UploadFile(filename="synthetic.mp4", file=io.BytesIO(b"replacement"))
    with pytest.raises(FileExistsError):
        asyncio.run(app._save_upload_with_limit(upload, str(marker)))
    assert marker.read_bytes() == b"original"


def test_upload_read_failure_cleans_only_new_partial_file(isolated_app, tmp_path):
    partial = tmp_path / "partial.mp4"
    class BrokenUpload:
        calls = 0
        async def read(self, size):
            self.calls += 1
            if self.calls > 1:
                raise OSError("synthetic read failure")
            return b"partial"
    with pytest.raises(OSError):
        asyncio.run(app._save_upload_with_limit(BrokenUpload(), str(partial)))
    assert not partial.exists()


def test_client_filename_is_not_a_storage_path(isolated_app):
    uploads, _, _ = isolated_app
    path = app._upload_path(SimpleNamespace(filename="../../outside.mp4"), "safe")
    from pathlib import Path
    assert Path(path).parent == uploads
    assert "outside" not in Path(path).name


def test_actor_path_escape_does_not_enqueue(monkeypatch, isolated_app, tmp_path):
    marker = tmp_path / "marker.png"
    marker.write_bytes(b"synthetic")
    request = app.SaaSGenerateRequest(script={}, selected_actor_url=str(marker))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.saasshorts_generate(request, "fake-fal", "fake-elevenlabs"))
    assert exc.value.status_code == 400
    assert app.saas_jobs == {}


def test_api_token_is_required_and_accepts_authenticated_remote_peer(monkeypatch, isolated_app):
    monkeypatch.setenv("OPENSHORTS_API_TOKEN", "synthetic-access-token")
    assert _http_get("/api/status/synthetic-missing").status_code == 401
    assert _http_get("/api/status/synthetic-missing", headers={"X-OpenShorts-Token": "wrong"}).status_code == 401
    response = _http_get("/api/status/synthetic-missing", client_host="192.0.2.20", headers={"X-OpenShorts-Token": "synthetic-access-token"})
    assert response.status_code == 404


def test_local_origin_works_and_foreign_host_is_rejected(isolated_app):
    response = _http_get("/api/status/synthetic-missing", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert _http_get("/api/status/synthetic-missing", headers={"Host": "attacker.example"}).status_code == 400


def test_streaming_json_body_is_limited_before_processing(isolated_app):
    async def run():
        async def chunks():
            for _ in range(17):
                yield b" " * 65536
        transport = httpx.ASGITransport(app=app.app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            return await client.post("/api/process", content=chunks(), headers={"Content-Type": "application/json", "X-Gemini-Key": "fake-key"})
    assert asyncio.run(run()).status_code == 413


def test_full_queue_rejects_work_before_probe(monkeypatch, isolated_app):
    monkeypatch.setattr(app, "jobs", {str(i): {"status": "queued"} for i in range(app.MAX_PENDING_JOBS)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.process_endpoint(SimpleNamespace(headers={}), None, None, None, None, None))
    assert exc.value.status_code == 429


def test_script_segment_count_is_bounded(isolated_app):
    req = app.SaaSGenerateRequest(script={"segments": [{}] * 31})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.saasshorts_generate(req, "fake-fal", "fake-elevenlabs"))
    assert exc.value.status_code == 400
    assert app.saas_jobs == {}


def test_active_saas_job_cannot_be_retried_concurrently(isolated_app):
    app.saas_jobs["existing-job"] = {"status": "processing"}
    req = app.SaaSGenerateRequest(script={}, retry_job_id="existing-job")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app.saasshorts_generate(req, "fake-fal", "fake-elevenlabs"))
    assert exc.value.status_code == 409


def test_editor_repair_ignores_raw_model_filter():
    from editor import VideoEditor
    editor = object.__new__(VideoEditor)
    editor.model_name = "synthetic"
    editor.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text='{"filter_string":"movie=/synthetic/marker"}')))
    assert editor._repair_filter("null", "synthetic failure", 320, 240) is None


def test_editor_repair_uses_bounded_effect_builder():
    from editor import VideoEditor
    editor = object.__new__(VideoEditor)
    editor.model_name = "synthetic"
    editor.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text='{"edits":[{"type":"movie=/marker","start":0,"end":10},{"type":"color_pop","start":1,"end":2,"strength":10000}]}')))
    result = editor._repair_filter("null", "synthetic failure", 320, 240, duration=10)
    assert result and "eq=" in result
    assert "movie" not in result and "10000" not in result


@pytest.mark.parametrize("root_path", ["/prefix", "/nested/prefix"])
def test_mounted_api_keeps_authentication_and_origin_guards(monkeypatch, isolated_app, root_path):
    async def run(headers=None):
        transport = httpx.ASGITransport(app=app.app, client=("192.0.2.20", 1234), root_path=root_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            return await client.get(f"{root_path}/api/status/synthetic-missing", headers=headers)
    assert asyncio.run(run()).status_code == 403
    monkeypatch.setenv("OPENSHORTS_API_TOKEN", "synthetic-access-token")
    assert asyncio.run(run()).status_code == 401
    assert asyncio.run(run({"X-OpenShorts-Token": "synthetic-access-token"})).status_code == 404
    assert asyncio.run(run({"X-OpenShorts-Token": "synthetic-access-token", "Origin": "https://attacker.example"})).status_code == 403


def test_recovery_ignores_unrelated_output_directories(isolated_app):
    _, output, _ = isolated_app
    (output / "benign folder").mkdir()
    (output / "other-öutput").mkdir()
    job = output / "valid-job"
    job.mkdir()
    (job / "job_state.json").write_text('{"job_id":"valid-job","status":"completed"}', encoding="utf-8")
    app._recover_jobs_from_disk()
    assert set(app.jobs) == {"valid-job"}


def test_backend_builder_copies_constraints_before_installation():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    lines = (repo / "Dockerfile").read_text(encoding="utf-8").splitlines()
    copied = set()
    for line in lines:
        if line.startswith("COPY ") and "--from" not in line:
            copied.update(line.split()[1:-1])
        if "pip install --no-cache-dir -r requirements.txt" in line:
            assert {"requirements.txt", "security-constraints.txt"} <= copied
            break
    else:
        pytest.fail("Backend dependency installation step was not located")


def _mock_public_response(monkeypatch, *, headers=None, chunks=(b"x" * 2048,)):
    real_client = httpx.Client
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks
    def respond(request):
        return httpx.Response(200, headers=headers or {"Content-Type": "image/png"}, stream=Stream())
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))


def test_download_overflow_keeps_existing_destination(monkeypatch, tmp_path):
    from security import download_public_media
    target = tmp_path / "image.png"
    target.write_bytes(b"original marker")
    _mock_public_response(monkeypatch, chunks=(b"x" * 1024, b"y" * 1024))
    with pytest.raises(ValueError, match="size"):
        download_public_media("https://93.184.216.34/image", str(target), kind="image", max_bytes=1024)
    assert target.read_bytes() == b"original marker"
    assert list(tmp_path.iterdir()) == [target]


def test_download_rejects_oversized_declared_length(monkeypatch, tmp_path):
    from security import download_public_media
    _mock_public_response(monkeypatch, headers={"Content-Type": "image/png", "Content-Length": "99999999"})
    with pytest.raises(ValueError, match="size"):
        download_public_media("https://93.184.216.34/image", str(tmp_path / "image.png"), kind="image", max_bytes=4096)
    assert list(tmp_path.iterdir()) == []


def test_download_writes_valid_bounded_media(monkeypatch, tmp_path):
    from security import download_public_media
    _mock_public_response(monkeypatch)
    target = tmp_path / "image.png"
    assert download_public_media("https://93.184.216.34/image", str(target), kind="image", max_bytes=4096) == str(target)
    assert target.read_bytes() == b"x" * 2048
    assert list(tmp_path.iterdir()) == [target]


def test_download_rejects_compressed_response(monkeypatch):
    from security import public_text
    _mock_public_response(monkeypatch, headers={"Content-Encoding": "gzip"}, chunks=(b"synthetic",))
    with pytest.raises(ValueError, match="Encoded"):
        public_text("https://93.184.216.34/page")


def test_provider_response_cannot_redirect_credentials(monkeypatch):
    real_client = httpx.Client
    seen = []
    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"request_id": "synthetic", "status_url": "https://attacker.example/status", "response_url": "https://queue.fal.run/synthetic"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ValueError, match="credentials"):
        saasshorts._fal_run("synthetic-model", {}, "fake-key")
    assert seen == ["https://queue.fal.run/synthetic-model"]


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows cmd.exe launcher regression")
def test_launcher_quotes_checkout_path_metacharacters(tmp_path):
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    folder = tmp_path / "checkout & echo UNSAFE_CHECKOUT_MARKER & rem_sentinel"
    folder.mkdir()
    old = subprocess.check_output(["git", "show", "98afa9ea5e9a842eb358834fe0cbd251d335573b:start.bat"], cwd=repo, text=True)
    new = (repo / "start.bat").read_text(encoding="utf-8")
    results = []
    for source in (old, new):
        line = next(line for line in source.splitlines() if line.startswith('start "OpenShorts Backend"'))
        # Exercise the actual launcher's path handling, replacing only the
        # service with echo and disabling windows/persistence for the test.
        line = line.replace('start "OpenShorts Backend"', 'start "" /B /WAIT').replace('cmd /k', 'cmd /d /c')
        line = line.replace(r'.\venv\Scripts\uvicorn app:app --host 0.0.0.0 --port 8000', 'echo SAFE_LAUNCH_MARKER')
        line = line.replace(r'.\venv\Scripts\uvicorn app:app --host 127.0.0.1 --port 8000', 'echo SAFE_LAUNCH_MARKER')
        script = folder / "audit-launch.cmd"
        script.write_text("@echo off\n" + line + "\n", encoding="ascii")
        result = subprocess.run(["cmd", "/d", "/c", script.name], cwd=folder, capture_output=True, text=True, timeout=15)
        results.append(result.stdout)
    assert "UNSAFE_CHECKOUT_MARKER" in results[0]
    assert "UNSAFE_CHECKOUT_MARKER" not in results[1]
    assert "SAFE_LAUNCH_MARKER" in results[1]
