"""ASGI bearer auth for the Streamable HTTP MCP mount."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from messjar.auth import (
    AuthContext,
    configured_password,
    extract_password,
    passwords_match,
)
from messjar.mcp_protocol import set_mcp_auth, set_mcp_store
from messjar.store import Store


def resolve_token(store: Store, provided: str | None) -> AuthContext:
    if not provided:
        if os.environ.get("MESSJAR_OPEN", "").lower() in ("1", "true", "yes"):
            return AuthContext(open_bus=True)
        raise PermissionError("unauthorized: provide agent key or jar password")
    admin = configured_password()
    if admin and passwords_match(provided, admin):
        return AuthContext(admin=True)
    agent_id = store.get_agent_by_token(provided)
    if agent_id:
        return AuthContext(agent_id=agent_id)
    jar = store.get_jar_by_password(provided)
    if jar:
        return AuthContext(jar_id=jar.id, jar_name=jar.name)
    raise PermissionError("unauthorized: bad or missing password")


def mcp_auth_middleware(store: Store) -> type:
    """Return Starlette-compatible middleware class closed over `store`."""

    class McpAuthMiddleware:
        def __init__(self, app: Callable) -> None:
            self.app = app

        async def __call__(
            self, scope: dict[str, Any], receive: Callable, send: Callable
        ) -> None:
            if scope["type"] not in ("http", "websocket"):
                await self.app(scope, receive, send)
                return

            set_mcp_store(store)
            headers = {
                k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
            }
            provided = extract_password(
                headers.get("authorization"),
                headers.get("x-messjar-password"),
            )
            try:
                auth = resolve_token(store, provided)
                set_mcp_auth(auth)
            except PermissionError as e:
                if scope["type"] != "http":
                    return
                body = json.dumps({"error": str(e)}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

            await self.app(scope, receive, send)

    return McpAuthMiddleware
