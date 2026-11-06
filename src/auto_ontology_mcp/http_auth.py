"""Shared-secret protection for the HTTP transport.

ChatGPT and claude.ai custom connectors accept either OAuth or no
authentication, so a bearer header alone cannot be configured there. The
secret can therefore travel three ways; the first two suit clients that let
you set headers, the third suits a pasted connector URL:

- ``Authorization: Bearer <token>``
- ``?key=<token>`` on the MCP URL
- a path prefix: ``https://host/<token>/mcp``
"""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import parse_qs

OPEN_PATHS = ("/health",)


class TokenMiddleware:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token
        self.prefix = f"/{token}"

    def _ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())  # type: ignore[union-attr]

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in OPEN_PATHS or path.startswith("/.well-known/"):
            await self.app(scope, receive, send)
            return
        if path == self.prefix or path.startswith(self.prefix + "/"):
            rest = path[len(self.prefix):] or "/"
            scope = dict(scope, path=rest, raw_path=rest.encode())
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        query = parse_qs(scope.get("query_string", b"").decode())
        key = (query.get("key") or query.get("token") or [None])[0]
        if self._ok(bearer) or self._ok(key):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized", "hint": "Pass the server token as a Bearer header, ?key=, or a /<token>/mcp path."}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
