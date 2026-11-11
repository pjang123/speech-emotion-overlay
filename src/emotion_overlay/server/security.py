"""HTTP hardening for a server that only ever talks to the local machine.

Binding to 127.0.0.1 keeps other computers out, but any web page open in the
user's browser can still send requests to localhost. These layers close that gap:

- Host check: rejects requests whose Host header isn't localhost (DNS rebinding).
- Origin check: rejects state-changing requests and WebSocket connections from
  other origins (CSRF and cross-site WebSocket hijacking, which would otherwise
  let any website read live transcripts).
- Security headers: CSP, nosniff, no framing, no referrer.
"""

from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from emotion_overlay.config import ServerConfig

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_hosts(server: ServerConfig) -> list[str]:
    return sorted({"127.0.0.1", "localhost", server.host})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp, hosts: list[str], port: int) -> None:
        self.app = app
        self.hosts = set(hosts)
        self.port = port

    def is_allowed(self, origin: str) -> bool:
        try:
            parts = urlsplit(origin)
            port = parts.port or 80
        except ValueError:
            return False
        return parts.scheme == "http" and parts.hostname in self.hosts and port == self.port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        guarded = kind == "websocket" or (kind == "http" and scope["method"] in UNSAFE_METHODS)
        if guarded:
            # Non-browser clients (curl, tests) send no Origin; browsers always do here.
            origin = _header(scope, b"origin")
            if origin is not None and not self.is_allowed(origin):
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("Cross-origin request blocked", 403)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, port: int) -> None:
        self.app = app
        ws = f"ws://127.0.0.1:{port} ws://localhost:{port}"
        csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            f"connect-src 'self' {ws}; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        self.headers = [
            (b"content-security-policy", csp.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], *self.headers]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install_security(app: FastAPI, server: ServerConfig) -> None:
    hosts = allowed_hosts(server)
    # Added last = runs first: host check, then origin check, then headers on the way out.
    app.add_middleware(SecurityHeadersMiddleware, port=server.port)
    app.add_middleware(OriginCheckMiddleware, hosts=hosts, port=server.port)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
