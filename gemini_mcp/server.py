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
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server import MCPServer

from .cookie_extractor import cookies_for_gemini
from .gemini_client import DEFAULT_METADATA, MediaRef, GeminiWebClient, GeminiWebError

log = logging.getLogger("gemini_mcp")

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

_client: Optional[GeminiWebClient] = None
_browser: str = "chrome"
_profile: str = "Default"
_language: str = "zh-CN"
_cookie_file: Optional[str] = None
_metadata_cache: dict[str, list[Any]] = {}  # conversation_id -> metadata

# How often to rotate __Secure-1PSIDTS in the background (seconds).
_ROTATE_INTERVAL = 25 * 60


class _TokenAuthMiddleware:
    """ASGI middleware requiring ``Authorization: Bearer <token>`` on HTTP.

    Only HTTP requests are gated; lifespan and websocket scopes pass through
    untouched. ``token=None`` disables the check (opt-in via
    ``--http-allow-anonymous``). The comparison is constant-time.
    """

    def __init__(self, app: Any, token: Optional[str]) -> None:
        self.app = app
        self._expected = b"Bearer " + token.encode() if token else None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._expected is not None:
            headers = dict(scope.get("headers") or [])
            if not hmac.compare_digest(
                headers.get(b"authorization", b""), self._expected
            ):
                await self._deny(send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send: Any) -> None:
        body = b'{"error": "invalid_token", "error_description": "Authentication required"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer error="invalid_token"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _run_http_async(host: str, port: int, token: Optional[str]) -> None:
    """Run the streamable-http transport behind the bearer-token middleware."""
    import uvicorn

    app = _TokenAuthMiddleware(mcp.streamable_http_app(host=host), token)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


# Well-known fallback cookie location: marketplace users just drop their
# exported cookies.json here — no env vars or patch edits required.
DEFAULT_COOKIE_FILE = Path.home() / ".config" / "gemini-web-mcp" / "cookies.json"

# Whether the current session's cookies were loaded from a file (vs. browser
# auto-extraction). _persist_cookies only writes when this is true or when an
# explicit file path was configured — an auto-extract session must never
# silently create a plaintext credential file.
_cookies_from_file = False


def _cookie_file_path() -> Optional[str]:
    """Resolve the cookie file: CLI arg > GEMINI_COOKIE_FILE > default path."""
    path = _cookie_file or os.environ.get("GEMINI_COOKIE_FILE")
    if path:
        return path
    return str(DEFAULT_COOKIE_FILE) if DEFAULT_COOKIE_FILE.is_file() else None


def _load_cookies() -> tuple[dict[str, str], list[str]]:
    """Load cookies: explicit file (CLI / env / default path) first, then
    auto-extract from the local browser profile."""
    global _cookies_from_file
    _cookies_from_file = False
    path = _cookie_file_path()
    if path and Path(path).is_file():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return {}, [f"cookie file is not readable JSON: {path} ({e})"]
        if isinstance(raw, dict):
            _cookies_from_file = True
            return raw, []
        if isinstance(raw, list):
            _cookies_from_file = True
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


def _persist_cookies(client: GeminiWebClient) -> None:
    """Write the (rotated) session cookies back to the cookie file, so a
    process restart keeps using fresh cookies. Skips sessions that loaded
    cookies purely from browser auto-extraction (no explicit file), so a
    plaintext credential file is never created without the user's choice."""
    if not (_cookies_from_file or _cookie_file or os.environ.get("GEMINI_COOKIE_FILE")):
        return
    path = _cookie_file_path()
    if not path:
        return
    entries = []
    for cookie in client.session.cookies:
        if "google.com" in (cookie.domain or ""):
            entries.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                }
            )
    if entries:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(
                json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.chmod(path, 0o600)
            log.info("Rotated cookies persisted to %s (%d cookies)", path, len(entries))
        except OSError as e:
            log.warning("Failed to persist rotated cookies: %s", e)


def _auto_rotate_loop() -> None:
    """Background daemon: renew the short-lived 1PSIDTS cookie periodically."""
    while True:
        time.sleep(_ROTATE_INTERVAL)
        try:
            client = _get_client()
            if client.rotate_cookies(force=True):
                _persist_cookies(client)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.warning("Background cookie rotation failed", exc_info=True)


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
        status["cookie_rotation"] = {
            "last_rotate_seconds_ago": (
                round(time.time() - client._last_rotate_at)
                if client._last_rotate_at else None
            ),
            "interval_seconds": _ROTATE_INTERVAL,
            "persist_to": _cookie_file_path() if _cookies_from_file else None,
        }
    except Exception as e:  # noqa: BLE001 - diagnostics tool must not crash
        status["errors"] = [str(e)]
    return status


@mcp.tool()
def gemini_send_message(
    message: str,
    conversation_id: Optional[str] = None,
    response_id: Optional[str] = None,
    language: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Send a message to Gemini (web interface), optionally attaching files.

    Without conversation_id a brand-new chat is started. Pass the
    conversation_id (and optionally response_id) from a previous result to
    continue that conversation.

    Args:
        message: The prompt to send.
        conversation_id: Continue this conversation (from a previous call).
        response_id: The response id to reply under (from a previous call).
        language: UI/reply language hint, e.g. "zh-CN" (default), "en".
        file_paths: Local paths of images/videos to attach (uploaded to
            Gemini and analyzed together with the message).

    Returns:
        The model's answer plus conversation_id/response_id/candidate_id to
        continue the chat later, and any web images cited in the reply.
    """
    if not message or not message.strip():
        raise ValueError("message must not be empty")
    client = _get_client()
    if language and language != client.language:
        client.language = language
    meta = _metadata_for(conversation_id, response_id)
    result = client.generate(message.strip(), metadata=meta, files=file_paths)
    _remember(result)
    return result.to_dict()


@mcp.tool()
def gemini_analyze_media(
    file_path: str,
    prompt: str = (
        "请客观描述这张图片/这个视频的可见内容：画面主体与物品、场景与背景、"
        "构图、光线与色彩风格、出现的文字或标识。请直接陈述视觉事实，"
        "按条目分点说明。"
    ),
    conversation_id: Optional[str] = None,
    response_id: Optional[str] = None,
) -> dict[str, Any]:
    """Use Gemini's eyes: upload one image/video and get a description.

    This is the tool an agent should call when the user uploads an image or
    video — Gemini looks at it and the result text comes back here.

    Tip: the more specific the prompt, the more likely Gemini answers — if a
    broad request is refused, retry with an objective angle (composition,
    lighting, colors, objects, text).

    Args:
        file_path: Local path of the image or video file.
        prompt: What to ask about the media (default: objective visual facts).
        conversation_id: Optional conversation to continue within.

    Returns:
        {text, media (cited web images), conversation_id, response_id}.
    """
    if not file_path:
        raise ValueError("file_path must not be empty")
    client = _get_client()
    meta = _metadata_for(conversation_id, response_id)
    result = client.generate(prompt, metadata=meta, files=[file_path])
    _remember(result)
    return result.to_dict()


@mcp.tool()
def gemini_generate_image(
    prompt: str,
    reference_image: Optional[str] = None,
    save_dir: str = "./media",
    conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Ask Gemini to generate an image (Imagen) from a text prompt.

    Optionally pass a reference_image (local path) for image-to-image edits.

    Args:
        prompt: What image to generate, e.g. "一只在月球上打伞的猫，水彩风格".
        reference_image: Optional local image path to edit / use as reference.
        save_dir: Directory the generated image is downloaded to.
        conversation_id: Optional existing conversation to generate within.

    Returns:
        {text, generated images (local paths + URLs), conversation_id}.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must not be empty")
    client = _get_client()
    files = [reference_image] if reference_image else None
    meta = _metadata_for(conversation_id, None)
    result = client.generate(prompt.strip(), metadata=meta, files=files)
    _remember(result)
    return _with_saved_media(result, save_dir, kinds={"generated_image"})


@mcp.tool()
def gemini_generate_video(
    prompt: str,
    save_dir: str = "./media",
    conversation_id: Optional[str] = None,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Ask Gemini to generate a video (Veo) from a text prompt.

    Video generation is asynchronous: this tool sends the request, then polls
    the conversation until Gemini finishes rendering and returns the video URL.

    Args:
        prompt: What video to generate, e.g. "一只橘猫在窗台上看下雨，电影感".
        save_dir: Directory the generated video is downloaded to.
        conversation_id: Optional existing conversation to generate within.
        timeout_seconds: Max time to wait for rendering (default 600s).

    Returns:
        {text, generated video (local path + URL), conversation_id}.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must not be empty")
    client = _get_client()
    meta = _metadata_for(conversation_id, None)
    result = client.generate(prompt.strip(), metadata=meta)
    _remember(result)
    videos = [m for m in result.media if m.kind == "generated_video"]

    # Poll read_conversation while the video is still rendering.
    cid = result.conversation_id
    deadline = time.monotonic() + timeout_seconds
    while not videos and cid and time.monotonic() < deadline:
        time.sleep(15)
        try:
            history = client.read_conversation(cid, limit=3)
            for turn in history.get("turns", []):
                for item in turn.get("media", []) or []:
                    if item["kind"] == "generated_video" and item.get("url"):
                        videos.append(
                            MediaRef(kind="generated_video", url=item["url"])
                        )
        except Exception:  # noqa: BLE001 - keep polling on transient errors
            continue
    if not videos:
        return {**result.to_dict(), "generated_video": None,
                "note": "Gemini did not return a video yet within the timeout. "
                        "Check the conversation later with gemini_read_conversation."}
    result.media = [m for m in result.media if m.kind != "generated_video"] + videos
    return _with_saved_media(result, save_dir, kinds={"generated_video"})


@mcp.tool()
def gemini_download_media(
    url: str,
    save_path: str,
) -> dict[str, str]:
    """Download a media URL (googleusercontent.com) to a local file.

    Some media URLs only work with the logged-in session cookies; this tool
    downloads them through the authenticated session.

    Args:
        url: The media URL (e.g. from a generated image result).
        save_path: Where to save the file (a .png/.mp4 suffix is added if missing).

    Returns:
        {path: local file path}
    """
    if not url or not save_path:
        raise ValueError("url and save_path must not be empty")
    path = _get_client().download_media(url, save_path)
    return {"path": path}


def _with_saved_media(
    result: Any, save_dir: str, kinds: set[str]
) -> dict[str, Any]:
    """Download generated media into save_dir and return result dict with local paths."""
    import os

    out = result.to_dict()
    saved = []
    for i, m in enumerate(out.get("media", [])):
        if m["kind"] not in kinds:
            continue
        ext = ".mp4" if m["kind"] == "generated_video" else ".png"
        dest = os.path.join(save_dir, f"{m['kind']}_{result.conversation_id or 'new'}_{i}{ext}")
        try:
            local = _get_client().download_media(m["url"], dest)
            m["local_path"] = local
            saved.append(m)
        except Exception as e:  # noqa: BLE001 - report download failure, keep URL
            m["download_error"] = str(e)
            saved.append(m)
    out["saved_media"] = saved
    return out


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
    global _browser, _profile, _language, _cookie_file

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
    parser.add_argument("--http-token", default=None,
                        help="Bearer token required for --transport http. Defaults to "
                             "GEMINI_HTTP_TOKEN, then to a randomly generated token "
                             "printed at startup.")
    parser.add_argument("--http-allow-anonymous", action="store_true",
                        help="Disable HTTP token auth. DANGEROUS: any local process "
                             "(and any website you visit) can drive the logged-in "
                             "Google account through the HTTP port.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.cookie_file:
        os.environ["GEMINI_COOKIE_FILE"] = args.cookie_file
    _cookie_file = args.cookie_file or os.environ.get("GEMINI_COOKIE_FILE")
    _browser, _profile, _language = args.browser, args.profile, args.language

    # Background daemon keeps the short-lived __Secure-1PSIDTS fresh by
    # rotating it through accounts.google.com/RotateCookies every 25 minutes
    # and persisting the new cookies back to the cookie file.
    import threading

    threading.Thread(target=_auto_rotate_loop, daemon=True, name="gemini-cookie-rotator").start()

    if args.transport == "http":
        import asyncio
        import secrets

        token: Optional[str] = args.http_token or os.environ.get("GEMINI_HTTP_TOKEN")
        if args.http_allow_anonymous:
            token = None
            log.warning(
                "HTTP token auth disabled (--http-allow-anonymous): any local "
                "process can drive this logged-in Google account."
            )
        elif not token:
            token = secrets.token_urlsafe(24)
            print(
                f"gemini-web-mcp HTTP auth token (send as `Authorization: Bearer "
                f"{token}`):",
                flush=True,
            )
        asyncio.run(_run_http_async("127.0.0.1", args.port, token))
    else:
        import asyncio

        asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
