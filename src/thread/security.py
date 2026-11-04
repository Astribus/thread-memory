"""Security headers, per-response CSP nonces, and client IP resolution behind Fly's proxy.

- All responses: HSTS (online), nosniff, Referrer-Policy: same-origin, frame-ancestors 'none',
  Permissions-Policy. (no-referrer would make browsers send "Origin: null" on same-origin form posts.)
- HTML responses: a CSP whose script-src allows only same-origin files and a nonce stamped onto each
  <script> tag. style-src keeps 'unsafe-inline' for inline style attributes.
- `client_ip` uses Fly-Client-IP on Fly; X-Forwarded-For's leftmost entry is client-controlled.
"""

import os
import re
import secrets

from starlette.requests import Request

SCRIPT_TAG = re.compile(rb"<script(?=[\s>])")


def client_ip(request: Request) -> str:
    """Client address for rate limiting. On Fly, Fly-Client-IP is set by the proxy and can't be spoofed."""
    if os.environ.get("FLY_APP_NAME"):
        ip = request.headers.get("fly-client-ip", "").strip()
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


def csp(nonce: str, path: str) -> str:
    parts = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
    # /login redirects to the client's callback, and browsers apply form-action to that redirect,
    # so form-action is omitted on that route.
    if path != "/login":
        parts.append("form-action 'self'")
    return "; ".join(parts)


class SecurityHeaders:
    """ASGI middleware. Buffers only text/html bodies (to inject the nonce); streams pass through."""

    def __init__(self, app, https: bool):
        self.app, self.https = app, https

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        nonce = secrets.token_urlsafe(16)
        path = scope.get("path", "")
        state = {"html": False, "start": None, "chunks": []}

        def common(headers: list) -> list:
            names = {k.lower() for k, _ in headers}
            extra = [
                (b"x-content-type-options", b"nosniff"),
                (b"referrer-policy", b"same-origin"),
                (b"x-frame-options", b"DENY"),
                (b"cross-origin-opener-policy", b"same-origin"),
                (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"),
            ]
            if self.https:
                extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
            return headers + [(k, v) for k, v in extra if k not in names]

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html"):
                    state["html"] = True
                    headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                    headers.append((b"content-security-policy", csp(nonce, path).encode()))
                    state["start"] = {**message, "headers": common(headers)}
                    return
                return await send({**message, "headers": common(headers)})
            if message["type"] == "http.response.body" and state["html"]:
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = SCRIPT_TAG.sub(b'<script nonce="' + nonce.encode() + b'"', b"".join(state["chunks"]))
                start = state["start"]
                start["headers"] = start["headers"] + [(b"content-length", str(len(body)).encode())]
                await send(start)
                return await send({"type": "http.response.body", "body": body})
            await send(message)

        await self.app(scope, receive, wrapped)
