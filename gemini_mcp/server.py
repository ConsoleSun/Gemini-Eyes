"""MCP server: lets any agent talk to gemini.google.com via the web interface.

Authentication comes from cookies decrypted out of a local Chrome/Edge
profile (see cookie_extractor.py), so no official Google API key is needed.

Run (stdio, default)::

    uv run gemini-mcp --browser chrome

Run over HTTP (SSE/streamable)::

    uv run gemini-mcp --transport http --port 8900

Tools exposed:

* gemini_send_message        — send a prompt (or continue a conversation)
* gemini_list_conversations  — recent conversations from the account
* gemini_read_conversation   — full history of one conversation
* gemini_delete_conversation — delete a conversation
* gemini_status              — cookie/token diagnostics
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver.server import MCPServer

from .cookie_extractor import cookies_for_gemini
from .gemini_client import DEFAULT_METADATA, GeminiWebClient, GeminiWebError

log = logging.getLogger("gemini_mcp")

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

_client: Optional[GeminiWebClient] = None
_browser: str = "chrome"
_profile: str = "Default"
_language: str = "zh-CN"
_metadata_cache: dict[str, list[Any]] = {}  # conversation_id -> metadata


def _load_cookies() -> tuple[dict[str, str], list[str]]:
    """Load cookies: explicit file first, then auto-extract from browser."""
    cookie_file = os.environ.get("GEMINI_COOKIE_FILE")
    if cookie_file and Path(cookie_file).is_file():
        raw = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw, []
        if isinstance(raw, list):
            return {c["name"]: c["value"] for c in raw if c.get("value")}, []
    return cookies_for_gemini(_browser, _profile)


def _get_client() -> GeminiWebClient:
    global _client
    if _client is not None:
        return _client
    cookies, errors = _load_cookies()
    if not cookies:
        raise GeminiWebError(
            "No Google cookies found. " + ("; ".join(errors) if errors else "")
        )
    _client = GeminiWebClient(cookies, language=_language)
    return _client


def _metadata_for(conversation_id: Optional[str], response_id: Optional[str]) -> Optional[list[Any]]:
    if not conversation_id:
        return None
    meta = list(_metadata_cache.get(conversation_id, DEFAULT_METADATA.copy()))
    meta[0] = conversation_id
    if response_id:
        meta[1] = response_id
    return meta


def _remember(result) -> None:
    if result.conversation_id and result.metadata:
        _metadata_cache[result.conversation_id] = result.metadata


mcp = MCPServer(
    "gemini-web",
    instructions=(
        "Bridges to gemini.google.com using the logged-in browser session "
        "(cookie-based auth, no API key). Use gemini_status to check auth, "
        "gemini_send_message to chat. To continue a conversation, pass the "
        "conversation_id (and optionally response_id) returned by the "
        "previous call."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# Required session cookies (full list Google may expect; only __Secure-1PSID
# is mandatory, __Secure-1PSIDTS is mandatory if present in your browser).
REQUIRED_COOKIES = [
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "__Secure-3PAPISID",
    "NID",
    "AEC",
    "SIDCC",
    "__Secure-1PSIDCC",
    "1P_JAR",
]


@mcp.tool()
def gemini_status() -> dict[str, Any]:
    """Diagnose the connection to gemini.google.com: cookie availability, token freshness and reachability."""
    import platform

    cookies, errors = _load_cookies()
    names = sorted(cookies)
    key_names = [n for n in REQUIRED_COOKIES if n in cookies]
    missing = [n for n in REQUIRED_COOKIES if n not in cookies]
    status: dict[str, Any] = {
        "ok": False,
        "browser": _browser,
        "profile": _profile,
        "platform": platform.system(),
        "cookie_count": len(cookies),
        "cookies_present": key_names,
        "cookies_missing": missing,
        "errors": errors,
    }
    if not cookies:
        status["errors"] = errors or ["no cookies available"]
        return status
    if "__Secure-1PSID" not in cookies:
        status["errors"] = [
            "The mandatory '__Secure-1PSID' session cookie is missing. "
            "Log in to gemini.google.com and export the cookies (Cookie-Editor "
            "extension), then pass them via --cookie-file."
        ]
        return status
    try:
        client = _get_client()
        client.init(force=True)
        status["ok"] = True
        status["token_fetched"] = True
        status["language"] = client.language
        status["session_id_present"] = bool(client.session_id)
    except Exception as e:  # noqa: BLE001 - diagnostics tool must not crash
        status["errors"] = [str(e)]
    return status


@mcp.tool()
def gemini_send_message(
    message: str,
    conversation_id: Optional[str] = None,
    response_id: Optional[str] = None,
    language: Optional[str] = None,
) -> dict[str, Any]:
    """Send a message to Gemini (web interface).

    Without conversation_id a brand-new chat is started. Pass the
    conversation_id (and optionally response_id) from a previous result to
    continue that conversation.

    Args:
        message: The prompt to send.
        conversation_id: Continue this conversation (from a previous call).
        response_id: The response id to reply under (from a previous call).
        language: UI/reply language hint, e.g. "zh-CN" (default), "en".

    Returns:
        The model's answer plus conversation_id/response_id/candidate_id to
        continue the chat later.
    """
    if not message or not message.strip():
        raise ValueError("message must not be empty")
    client = _get_client()
    if language and language != client.language:
        client.language = language
    meta = _metadata_for(conversation_id, response_id)
    result = client.generate(message.strip(), metadata=meta)
    _remember(result)
    return result.to_dict()


@mcp.tool()
def gemini_list_conversations(limit: int = 13) -> list[dict[str, Any]]:
    """List the most recent conversations of the logged-in account.

    Args:
        limit: How many conversations per bucket (pinned/unpinned) to fetch.

    Returns:
        A list of {conversation_id, title, pinned, timestamp}.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return _get_client().list_conversations(limit=limit)


@mcp.tool()
def gemini_read_conversation(conversation_id: str, limit: int = 10) -> dict[str, Any]:
    """Read the turns of an existing conversation (newest first).

    Args:
        conversation_id: e.g. "c_..." as returned by gemini_list_conversations.
        limit: Maximum number of turns to fetch.

    Returns:
        {conversation_id, turns: [{role, text, ...}]}
    """
    if not conversation_id:
        raise ValueError("conversation_id must not be empty")
    return _get_client().read_conversation(conversation_id, limit=limit)


@mcp.tool()
def gemini_delete_conversation(conversation_id: str) -> dict[str, str]:
    """Delete a conversation from Gemini history (irreversible).

    Args:
        conversation_id: e.g. "c_..." as returned by gemini_list_conversations.
    """
    if not conversation_id:
        raise ValueError("conversation_id must not be empty")
    _get_client().delete_conversation(conversation_id)
    _metadata_cache.pop(conversation_id, None)
    return {"deleted": conversation_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    global _browser, _profile, _language

    parser = argparse.ArgumentParser(
        prog="gemini-mcp",
        description="MCP server bridging agents to gemini.google.com via the web interface (cookie auth).",
    )
    parser.add_argument("--browser", choices=["chrome", "edge", "chromium"], default="chrome",
                        help="Browser whose profile holds the Google session (default: chrome).")
    parser.add_argument("--profile", default="Default",
                        help="Browser profile directory (default: Default).")
    parser.add_argument("--language", default="zh-CN",
                        help="Language hint sent to Gemini (default: zh-CN).")
    parser.add_argument("--cookie-file", default=None,
                        help="JSON file with cookies [{name,value,...}] or {name:value}; overrides browser extraction.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="MCP transport (default: stdio).")
    parser.add_argument("--port", type=int, default=8900,
                        help="HTTP port when --transport http (default: 8900).")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.cookie_file:
        os.environ["GEMINI_COOKIE_FILE"] = args.cookie_file
    _browser, _profile, _language = args.browser, args.profile, args.language

    if args.transport == "http":
        import asyncio

        asyncio.run(
            mcp.run_streamable_http_async(host="127.0.0.1", port=args.port)
        )
    else:
        import asyncio

        asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
