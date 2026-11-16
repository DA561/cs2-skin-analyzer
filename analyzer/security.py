import hashlib
import hmac
import os
import secrets
import time
from collections import deque

from starlette.responses import JSONResponse

MAX_BODY = 12 * 1024 * 1024
PUBLIC = {"/api/health", "/api/status", "/api/score", "/api/actions-schema"}


class Guard:
    def __init__(self, app):
        self.app = app
        self.calls = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        protected = (path.startswith("/api/") and path not in PUBLIC) or path == "/mcp"
        headers = {k.lower(): v for k, v in scope["headers"]}
        if protected:
            expected = os.getenv("ANALYZER_ACCESS_KEY", "")
            supplied = headers.get(b"x-access-key", b"").decode("latin-1")
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            if authorization.startswith("Bearer "):
                supplied = authorization[7:]
            if not expected:
                return await JSONResponse({"detail": "access_key_not_configured"}, status_code=503)(
                    scope, receive, send
                )
            if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
                return await JSONResponse({"detail": "Unauthorized"}, status_code=401)(scope, receive, send)
            scope.setdefault("state", {})["owner"] = hmac.new(
                expected.encode(), b"analyzer-owner", hashlib.sha256
            ).hexdigest()
            at = time.monotonic()
            while self.calls and self.calls[0] < at - 60:
                self.calls.popleft()
            if len(self.calls) >= 60:
                return await JSONResponse(
                    {"detail": "request_rate_limited"}, status_code=429, headers={"Retry-After": "60"}
                )(scope, receive, send)
            self.calls.append(at)
        # Buffer with an enforced ceiling even without Content-Length, before JSON or image decoding.
        if scope.get("method") in ("POST", "PUT", "PATCH"):
            try:
                size = int(headers.get(b"content-length", b"0"))
            except ValueError:
                size = MAX_BODY + 1
            if size > MAX_BODY or size < 0:
                return await JSONResponse({"detail": "body_too_large"}, status_code=413)(scope, receive, send)
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > MAX_BODY:
                    return await JSONResponse({"detail": "body_too_large"}, status_code=413)(scope, receive, send)
                if not event.get("more_body", False):
                    break
            delivered = False
            original_receive = receive

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            return await self.app(scope, buffered_receive, send)
        return await self.app(scope, receive, send)
