"""HTTP perimeter for a local, single-operator media workstation."""
import hmac
import ipaddress
import os

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette._utils import get_route_path

from security import MEDIA_EXTENSIONS


def configured_origins():
    defaults = [f"http://{host}:{port}" for host in ("localhost", "127.0.0.1", "[::1]") for port in (5173, 5175, 8000)]
    return [item.strip() for item in os.environ.get("OPENSHORTS_ALLOWED_ORIGINS", ",".join(defaults)).split(",") if item.strip()]


class MediaFiles(StaticFiles):
    async def get_response(self, path, scope):
        # output/ also holds transcripts, checkpoint state and provider errors.
        # Those files must never be exposed by the media mount.
        normalized = path.replace("\\", "/")
        if any(part.startswith(".") for part in normalized.split("/")) or os.path.splitext(normalized)[1].lower() not in MEDIA_EXTENSIONS:
            raise HTTPException(status_code=404, detail="Media not found")
        return await super().get_response(path, scope)


class SecurityBoundaryMiddleware:
    def __init__(self, app, max_body_bytes):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.active_requests = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        path = get_route_path(scope)
        protected = path.startswith(("/api", "/videos", "/thumbnails", "/docs", "/redoc", "/openapi"))
        # A browser never sends custom headers on a CORS preflight, so it can
        # not carry the token. CORSMiddleware answers it without reaching a
        # route; the origin check below still applies.
        preflight = (scope.get("method") == "OPTIONS" and b"origin" in headers
                     and b"access-control-request-method" in headers)
        if protected:
            token = os.environ.get("OPENSHORTS_API_TOKEN", "")
            peer = (scope.get("client") or ("", 0))[0]
            try:
                local = ipaddress.ip_address(peer).is_loopback
            except ValueError:
                local = False
            if preflight:
                pass
            elif token:
                supplied = headers.get(b"x-openshorts-token", b"")
                if not hmac.compare_digest(supplied, token.encode("utf-8")):
                    return await JSONResponse({"detail": "Access token required"}, status_code=401)(scope, receive, send)
            elif not local:
                return await JSONResponse({"detail": "Remote API access requires authentication"}, status_code=403)(scope, receive, send)
            origin = headers.get(b"origin", b"").decode("latin-1")
            if origin and origin not in configured_origins():
                return await JSONResponse({"detail": "Browser origin is not allowed"}, status_code=403)(scope, receive, send)

        content_type = headers.get(b"content-type", b"").lower()
        limit = self.max_body_bytes if b"multipart/form-data" in content_type else min(self.max_body_bytes, 1024 * 1024)
        if path in ("/api/thumbnail/generate", "/api/saasshorts/actor-upload"):
            limit = min(limit, 21 * 1024 * 1024)
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = -1
        if declared < 0 or declared > limit:
            return await JSONResponse({"detail": "Request body exceeds limit"}, status_code=413)(scope, receive, send)
        # Bound in-flight requests as well as work queued by the API.
        if self.active_requests >= 64:
            return await JSONResponse({"detail": "Server is busy"}, status_code=429)(scope, receive, send)
        self.active_requests += 1
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(status_code=413, detail="Request body exceeds limit")
            return message

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
            await send(message)

        try:
            await self.app(scope, bounded_receive, secure_send)
        finally:
            self.active_requests -= 1
