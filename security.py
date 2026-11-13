"""Small shared boundary checks; no network requests occur during validation."""
from contextlib import contextmanager
import ipaddress
import ntpath
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from urllib.parse import urljoin, urlsplit
import zlib

import httpx


SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MEDIA_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".wav", ".m4a", ".ogg"}


def validate_id(value):
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ValueError("Invalid identifier")
    return value


def confined_path(directory, relative):
    """Constrain POSIX and Windows input, including existing symlink targets."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("Invalid relative path")
    normalized = relative.replace("\\", "/")
    if ntpath.splitdrive(relative)[0] or normalized.startswith("/") or any(p in ("", ".", "..") for p in normalized.split("/")):
        raise ValueError("Invalid relative path")
    root = Path(directory).resolve()
    target = (root / normalized).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("Path escapes storage directory")
    return str(target)


def upload_suffix(filename):
    # The supplied basename is never used for storage.
    suffix = ntpath.splitext(filename or "")[1].lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("Unsupported media extension")
    return suffix


def validate_youtube_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("Provide a valid YouTube video URL")
    parsed = urlsplit(url)
    try:
        valid_port = parsed.port in (None, 80, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme not in ("https", "http") or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
            or parsed.username is not None or parsed.password is not None or not valid_port):
        raise ValueError("Only YouTube video URLs are supported")
    return url


def _public_target(url):
    if not isinstance(url, str) or len(url) > 4096 or "\\" in url or any(ord(c) < 33 for c in url):
        raise ValueError("Invalid public URL")
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host or parsed.userinfo:
        raise ValueError("Only public HTTP(S) URLs without credentials are supported")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Only standard HTTP(S) ports are supported")
    host = parsed.host
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)]
    if not addresses or any(not ip.is_global or ip.is_multicast or (isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None)) for ip in addresses):
        raise ValueError("URL destination must be a public unicast address")
    # Connect to the checked address, rather than resolving the hostname a second
    # time. Preserve hostname verification/SNI and the HTTP authority separately.
    return parsed, parsed.copy_with(host=str(addresses[0]))


@contextmanager
def public_stream(url, *, timeout=30.0, headers=None, accept_encoding="identity"):
    """GET public web content with DNS pinning and a check on every redirect.

    With accept_encoding other than "identity" the caller decodes the raw
    body itself (see public_text); the encoding check below is then skipped.
    """
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            original, target = _public_target(url)
            authority = original.netloc.decode("ascii")
            request_headers = {**(headers or {}), "Host": authority, "Accept-Encoding": accept_encoding}
            with client.stream("GET", target, headers=request_headers, extensions={"sni_hostname": original.host}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect has no destination")
                    url = urljoin(str(original), location)
                    continue
                response.raise_for_status()
                if accept_encoding == "identity" and response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise ValueError("Encoded download responses are not supported")
                yield response
                return
    raise ValueError("Too many download redirects")


def bounded_chunks(response, max_bytes, *, max_seconds=1800):
    length = response.headers.get("content-length")
    if length and (not length.isdecimal() or int(length) > max_bytes):
        raise ValueError("Download exceeds size limit")
    size = 0
    started = time.monotonic()
    # Raw chunks prevent a peer trickling bytes from hiding indefinitely inside
    # an iterator's fixed-size output buffer. These are the bytes on the wire:
    # public_stream rejects encoded bodies unless the caller decodes them.
    for chunk in response.iter_raw():
        size += len(chunk)
        if size > max_bytes or time.monotonic() - started > max_seconds:
            raise ValueError("Download exceeds size or time limit")
        yield chunk


def _text_decoder(content_encoding):
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return None
    if encoding in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj()
    raise ValueError("Encoded download responses are not supported")


def public_text(url, *, timeout=30, headers=None, max_bytes=5 * 1024 * 1024):
    """Fetch a public page as text. Many servers gzip HTML even when asked not to.

    Only gzip is requested: "deflate" is sent zlib-wrapped or raw depending on
    the server, and a raw stream would fail the zlib decoder.

    max_bytes caps both the bytes on the wire and the decoded text, and the
    decoder never produces more than the remaining budget per chunk, so a
    small compressed body cannot inflate past the limit in memory.
    """
    with public_stream(url, timeout=timeout, headers=headers, accept_encoding="gzip") as response:
        decoder = _text_decoder(response.headers.get("content-encoding", "identity"))
        parts = []
        size = 0
        for chunk in bounded_chunks(response, max_bytes, max_seconds=120):
            if decoder is not None:
                try:
                    chunk = decoder.decompress(chunk, max_bytes - size + 1)
                except zlib.error as exc:
                    raise ValueError("Invalid compressed response") from exc
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Download exceeds size or time limit")
            parts.append(chunk)
        if decoder is not None:
            try:
                tail = decoder.flush()
            except zlib.error as exc:
                raise ValueError("Invalid compressed response") from exc
            size += len(tail)
            if size > max_bytes:
                raise ValueError("Download exceeds size or time limit")
            parts.append(tail)
        return b"".join(parts).decode(response.encoding or "utf-8", errors="replace")


def download_public_media(url, output_path, *, kind="media", timeout=120, max_bytes=512 * 1024 * 1024):
    """Stream to a private temporary file; replace only after complete validation."""
    temp_path = None
    try:
        with public_stream(url, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "").lower()
            if kind == "image" and not content_type.startswith("image/"):
                raise ValueError("Expected an image response")
            if kind == "video" and not (content_type.startswith("video/") or "octet-stream" in content_type):
                raise ValueError("Expected a video response")
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(output_path)), prefix=".download-", delete=False) as target:
                temp_path = target.name
                for chunk in bounded_chunks(response, max_bytes):
                    target.write(chunk)
                if target.tell() < 1024:
                    raise ValueError("Downloaded media is too small")
            os.replace(temp_path, output_path)
            temp_path = None
        return output_path
    finally:
        if temp_path is not None:
            os.remove(temp_path)
