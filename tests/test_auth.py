"""Tests for the HTTP bearer-token middleware in server.py."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from gemini_mcp.server import _TokenAuthMiddleware


async def _fake_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _run(token: Optional[str], headers: Optional[dict[str, str]] = None) -> dict[str, list[Any]]:
    collected: dict[str, list[Any]] = {}
    # ASGI lowercases header names on the wire.
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        collected.setdefault(message["type"], []).append(message)

    mw = _TokenAuthMiddleware(_fake_app, token)
    asyncio.run(mw(scope, receive, send))
    return collected


def _status(collected: dict[str, list[Any]]) -> int:
    return collected["http.response.start"][0]["status"]


def test_rejects_missing_token() -> None:
    assert _status(_run("sekret", {})) == 401


def test_rejects_wrong_token() -> None:
    assert _status(_run("sekret", {"Authorization": "Bearer nope"})) == 401


def test_accepts_correct_token() -> None:
    collected = _run("sekret", {"Authorization": "Bearer sekret"})
    assert _status(collected) == 200
    assert collected["http.response.body"][0]["body"] == b"ok"


def test_token_comparison_is_prefix_safe() -> None:
    # A longer token that merely shares the prefix must not pass.
    assert _status(_run("sekret", {"Authorization": "Bearer sekret-extra"})) == 401


def test_anonymous_mode_passes_through() -> None:
    assert _status(_run(None, {})) == 200


def test_lifespan_scope_passes_through() -> None:
    seen: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    asyncio.run(_TokenAuthMiddleware(app, "sekret")({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
