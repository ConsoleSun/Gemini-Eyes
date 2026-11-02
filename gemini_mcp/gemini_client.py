"""Minimal client for gemini.google.com's internal RPC endpoints.

This replays exactly what the browser does, no official API key involved:

* GET  https://gemini.google.com/app  -> extracts SNlM0e (CSRF token), cfb2h
  (build label), FdrFJe (session id) from the HTML/JS payload.
* POST /_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate
  -> generates content (streaming, length-prefixed frames).
* POST /_/BardChatUi/data/batchexecute -> list/read/delete conversations.

All requests are authenticated by the cookies extracted from the local
browser profile (see cookie_extractor.py).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://gemini.google.com"
INIT_URL = f"{BASE_URL}/app"
GENERATE_URL = (
    f"{BASE_URL}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
)
BATCH_EXEC_URL = f"{BASE_URL}/_/BardChatUi/data/batchexecute"
UPLOAD_URL = "https://push.clients6.google.com/upload"
PUSH_ID = "feeds/mcudyrk2a4khkz"
ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"

# File-type integer used inside the StreamGenerate file entry (web app enum).
FILES_ENUM_INT = {
    "application/octet-stream": 0,
    "image": 1,
    "video": 2,
    "text": 3,
    "audio": 4,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 7,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 10,
    "application/pdf": 11,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": 12,
    "application/json": 16,
}

# Default 10-slot metadata used for brand-new conversations (mirrors the web app).
DEFAULT_METADATA: list[Any] = ["", "", "", None, None, None, None, None, None, ""]

# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------
#
# The web app picks a model through a side channel header. Without it the
# server silently answers with the account's default (Flash tier), no matter
# what the request payload says — measured on 2026-11-02: the payload's model
# slot alone (inner[79]) was ignored, while adding this header routed the same
# prompt to "3.1 Pro" (echoed back by the server at response inner[42]).
#
# ``x-goog-ext-525001261-jspb`` is a JSON list (zero-based indices):
#
#   4  model id hash            11  capacity / subscription tier
#   8  client capabilities      14  model number (routing key)
#   15 thinking level           16  request/session uuid
#
# The same model number is mirrored into the request payload at inner[79]
# (the front-end MODE_CATEGORY enum) and the thinking level at inner[80].
MODEL_HEADER_KEY = "x-goog-ext-525001261-jspb"

# (model_id, capacity, model_number) as observed on the 2026-11 web lineup.
# model_number == MODE_CATEGORY: 1=Flash, 2=Thinking, 3=Pro, 4=Auto,
# 5=dynamic thinking, 6=Flash-Lite, 8=Argon (Gemini 4).
MODEL_CATALOG: dict[str, tuple[str, int, int]] = {
    # Gemini 4 "Argon" — the highest tier the web UI now exposes. Measured
    # 2026-11-02: number 8 served as "4 Argon High" at thinking 2; the hash
    # and capacity slots do not change the routing (all catalog hashes and
    # capacities 1/4 answered "4 Argon"), so the Pro hash is reused.
    "argon": ("e6fa609c3fa255c0", 4, 8),
    # Gemini 3.1 Pro — second tier, kept as the first fallback.
    "pro": ("e6fa609c3fa255c0", 4, 3),
    "pro-advanced": ("e6fa609c3fa255c0", 2, 3),
    "pro-basic": ("9d8ca3786ebdfbea", 1, 3),
    # Flash / Flash-Lite — used as fallbacks when Pro is not usable.
    "flash": ("56fdd199312815e2", 4, 1),
    "flash-basic": ("fbb127bbb056c959", 1, 1),
    "flash-lite": ("8c46e95b1a07cecc", 4, 6),
    "flash-lite-basic": ("cf41b0e0dd7d53e5", 1, 6),
}

# User-facing aliases (also used with the "gemini-" prefix stripped).
MODEL_ALIASES: dict[str, str] = {
    "highest": "argon",
    "max": "argon",
    "4-argon": "argon",
    "argon-4": "argon",
    "4argon": "argon",
    "4": "argon",  # gemini-4 -> "4" after the prefix strip
    "3.1-pro": "pro",
    "3-pro": "pro",
    "pro-plus": "pro",
    "3.8-flash": "flash",
    "3.6-flash": "flash",
    "3.5-flash": "flash",
    "lite": "flash-lite",
    "legacy": "legacy",
    "default": "legacy",
    "off": "legacy",
    "none": "legacy",
    "disabled": "legacy",
}

# Highest tier by default: Gemini 4 "Argon".
DEFAULT_MODEL = "argon"
# Fallback chain when a tier is rejected (1037 usage limit / 1052 model
# unavailable): Argon -> 3.1 Pro -> Flash-Basic -> legacy model-less request.
# DEFAULT_MODEL_FALLBACK is kept as the single-step default (backward compat);
# the chain below is what the client actually walks.
DEFAULT_MODEL_FALLBACK = "pro"
DEFAULT_MODEL_FALLBACK_CHAIN: tuple[str, ...] = ("pro", "flash-basic")

# Native thinking levels (header index 15 / inner[80]):
#   1 = standard, 2 = extended, 3 = deep think.
# Level 3 ("Deep Think") is asynchronous: the first response is only a
# "processing, check back later" placeholder, so the highest level that still
# answers synchronously is 2 — hence the default.
THINKING_LEVELS: dict[str, int] = {
    "standard": 1,
    "low": 1,
    "minimal": 1,
    "extended": 2,
    "medium": 2,
    "high": 2,
    "max": 2,
    "highest": 2,
    "deep": 3,
    "deep_think": 3,
    "deepthink": 3,
}
DEFAULT_THINKING = 2

# Server-side error codes that mean "this model setting is not usable here".
# 1037 = usage limit for this model/account, 1052 = model header invalid or
# model unavailable. Both are retried with the next configured candidate.
MODEL_RETRY_CODES = frozenset({1037, 1052})
# 1050 = the conversation already carries a different model; only the legacy
# (model-less) request can continue it.
CONVERSATION_MODEL_CONFLICT = 1050

# ---------------------------------------------------------------------------
# Transport resilience (independent of the model fallback chain above)
# ---------------------------------------------------------------------------
#
# Two bounded recovery layers guard every HTTP exchange, so a dead token or a
# flaky connection degrades into one extra attempt instead of a hard failure:
#
# * session healing — HTTP 400/401/403 means SNlM0e / __Secure-1PSIDTS died
#   mid-flight; rotate the cookies and force a token bootstrap, then retry
#   exactly once (a second 400 is a real error, not a stale session).
# * transient retry — server code 1013, HTTP 5xx and transport errors
#   (timeouts, resets, mid-stream disconnects) are retried with backoff.
#
# Both are capped, so no loop can spin forever.

# HTTP statuses that mean "the session/cookies are stale", not "bad request".
SESSION_HEAL_STATUSES = frozenset({400, 401, 403})
# Server-side error codes that are worth retrying unchanged.
TRANSIENT_ERROR_CODES = frozenset({1013})
# Extra attempts after the first failure, and their backoff delays (seconds).
TRANSIENT_RETRY_ATTEMPTS = 2
TRANSIENT_RETRY_DELAYS: tuple[float, ...] = (0.8, 2.0)


@dataclass(frozen=True)
class ModelSetting:
    """One concrete model selection: catalog entry plus thinking level."""

    label: str
    model_id: str
    capacity: int
    model_number: int
    thinking: int

    def to_headers(self, session_id: str) -> dict[str, str]:
        """Build the side-channel request headers for this setting."""
        header = (
            f'[1,null,null,null,"{self.model_id}",null,null,0,[4,5,6,8],'
            f"null,null,{self.capacity},null,null,{self.model_number},"
            f'{self.thinking},"{session_id}"]'
        )
        return {
            MODEL_HEADER_KEY: header,
            "x-goog-ext-73010989-jspb": "[0]",
            "x-goog-ext-73010990-jspb": "[0,0,0]",
            "x-goog-ext-525005358-jspb": f'["{session_id}",1]',
        }


def _normalise_model_name(raw: str) -> str:
    key = raw.strip().lower().replace("_", "-")
    if key.startswith("gemini-"):
        key = key[len("gemini-") :]
    return MODEL_ALIASES.get(key, key)


def resolve_thinking_level(raw: Optional[Union[int, str]], default: int = DEFAULT_THINKING) -> int:
    """Map a user-provided thinking depth to the native level (1/2/3)."""
    if raw is None:
        return default
    if isinstance(raw, bool):  # bool is an int subclass — treat True as default
        return default
    if isinstance(raw, int):
        return raw if raw in (1, 2, 3) else default
    key = str(raw).strip().lower().replace("-", "_")
    if not key:
        return default
    if key in ("true", "on", "yes"):
        return default
    if key in ("false", "no"):
        return 1
    if key in THINKING_LEVELS:
        return THINKING_LEVELS[key]
    try:
        value = int(key)
    except ValueError:
        return default
    return value if value in (1, 2, 3) else default


def resolve_model_setting(
    model: Optional[str] = None,
    thinking: Optional[Union[int, str]] = None,
    capacity: Optional[Union[int, str]] = None,
) -> Optional[ModelSetting]:
    """Resolve a model name into a request setting.

    ``model`` accepts catalog names (``argon``, ``pro``, ``flash``, ``flash-lite``, ...),
    their ``gemini-*`` aliases, or a legacy switch (``legacy``/``default``/
    ``off``), which returns ``None``: the request then stays byte-for-byte the
    pre-model-selection payload (whatever the web app defaults to).

    Unknown names fall back to ``DEFAULT_MODEL`` (highest tier) so a typo in an
    env var degrades to the intended behaviour instead of the old default.
    """
    key = _normalise_model_name(model if model is not None else DEFAULT_MODEL)
    if key == "legacy":
        return None
    entry = MODEL_CATALOG.get(key)
    if entry is None:
        key = DEFAULT_MODEL
        entry = MODEL_CATALOG[key]
    model_id, default_capacity, model_number = entry

    cap = default_capacity
    if capacity is not None and str(capacity).strip():
        try:
            requested = int(str(capacity).strip())
        except ValueError:
            requested = 0
        if 1 <= requested <= 4:
            cap = requested

    return ModelSetting(
        label=key,
        model_id=model_id,
        capacity=cap,
        model_number=model_number,
        thinking=resolve_thinking_level(thinking),
    )


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "X-Same-Domain": "1",
    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
}

# RPC ids for the batchexecute endpoint (current web app).
GRPC_LIST_CHATS = "MaZiqc"
GRPC_READ_CHAT = "hNvQHb"
GRPC_DELETE_CHAT_1 = "GzXR5e"
GRPC_DELETE_CHAT_2 = "qWymEb"

# Known server error codes (from the web app's frontend).
ERROR_CODES = {
    1013: "temporary server error (1013), retry later",
    1037: "usage limit exceeded for this model/account, wait a bit",
    1050: "model inconsistent with conversation history",
    1052: "model header invalid or model unavailable",
    1060: "IP temporarily blocked by Google, try a proxy or wait",
}


class GeminiWebError(RuntimeError):
    """Raised for non-200 responses and known server error codes.

    ``transport=True`` marks an error that came from the HTTP layer itself
    (timeout, connection reset, mid-stream disconnect) rather than from an
    HTTP status or a server error code; those are retried with backoff.
    """

    def __init__(
        self, message: str, code: Optional[int] = None, *, transport: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.transport = transport

    @property
    def is_retryable(self) -> bool:
        """True for failures worth retrying unchanged (1013 / 5xx / transport)."""
        if self.transport:
            return True
        if self.code in TRANSIENT_ERROR_CODES:
            return True
        return isinstance(self.code, int) and 500 <= self.code < 600


@dataclass
class MediaRef:
    """A media item referenced or produced by Gemini."""

    kind: str  # "web_image" | "generated_image" | "generated_video"
    url: str = ""
    title: str = ""
    alt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "title": self.title, "alt": self.alt}


@dataclass
class GenerationResult:
    text: str = ""
    thoughts: str = ""
    conversation_id: str = ""
    response_id: str = ""
    candidate_id: str = ""
    completed: bool = False
    metadata: list[Any] = field(default_factory=list)
    media: list[MediaRef] = field(default_factory=list)
    raw_frames: list[Any] = field(default_factory=list)
    elapsed: float = 0.0
    # Model name the server says produced this answer (response inner[42],
    # e.g. "3.1 Pro"). Empty when the server did not report one.
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "thoughts": self.thoughts,
            "conversation_id": self.conversation_id,
            "response_id": self.response_id,
            "candidate_id": self.candidate_id,
            "completed": self.completed,
            "media": [m.to_dict() for m in self.media],
            "elapsed_seconds": round(self.elapsed, 2),
            "model": self.model,
        }


class FrameParser:
    """Incremental parser for Google's length-prefixed streaming frames.

    Each frame is: ``<char_count>\n<json>\n`` where char_count is measured in
    UTF-16 code units (JavaScript ``String.length``), matching the browser.
    """

    def __init__(self) -> None:
        self._buf = ""
        self.frames: list[Any] = []

    def feed(self, chunk: str) -> list[Any]:
        """Append ``chunk`` and return **only the frames parsed from it**.

        The return value used to be ``self.frames`` (every frame ever seen),
        which made every caller reprocess history on each chunk — duplicate
        ``raw_frames``, duplicate ``on_frame`` callbacks and duplicated media.
        ``self.frames`` is still kept as a debugging accumulator, but it is no
        longer what ``feed`` returns.
        """
        self._buf += chunk
        # Strip the anti-XSSI prefix once (">]}'") plus surrounding whitespace.
        if self._buf.startswith(")]}'"):
            self._buf = self._buf[4:].lstrip()
        new_frames: list[Any] = []
        pos = 0
        while pos < len(self._buf):
            # Frames are separated by newlines; skip whitespace between them.
            while pos < len(self._buf) and self._buf[pos].isspace():
                pos += 1
            if pos >= len(self._buf):
                break
            m = re.match(r"(\d+)\n", self._buf[pos:])
            if not m:
                break
            length = int(m.group(1))
            # Google's length marker counts UTF-16 units from the newline
            # after the digits THROUGH the newline after the JSON payload
            # (i.e. marker = 1 + len(json) + 1). So start counting at the
            # newline itself, not after it.
            start = pos + m.start() + len(m.group(1))
            # Walk the buffer once, spending the frame's UTF-16 budget as we
            # go. If the budget is not exhausted before the buffer ends the
            # frame is still incomplete: stop here and wait for the next chunk
            # (no separate full-buffer scan of self._buf[start:]).
            end = start
            remaining = length
            while remaining > 0 and end < len(self._buf):
                remaining -= 2 if ord(self._buf[end]) > 0xFFFF else 1
                end += 1
            if remaining > 0:
                break
            payload = self._buf[start:end].strip()
            pos = end
            if payload:
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, list):
                    new_frames.extend(obj)
                else:
                    new_frames.append(obj)
        self._buf = self._buf[pos:]
        self.frames.extend(new_frames)
        return new_frames


def get_nested(data: Any, path: list[Any], default: Any = None) -> Any:
    """Safe navigation through nested lists/dicts (like lodash get)."""
    cur = data
    for key in path:
        if isinstance(key, int) and isinstance(cur, list) and -len(cur) <= key < len(cur):
            cur = cur[key]
        elif isinstance(key, str) and isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur if cur is not None else default


class GeminiWebClient:
    """Authenticated client for gemini.google.com web RPCs."""

    def __init__(
        self,
        cookies: dict[str, str],
        language: str = "zh-CN",
        timeout: float = 180.0,
        token_ttl: float = 1800.0,
        on_frame: Optional[Callable[[Any], None]] = None,
        model: Optional[str] = None,
        thinking: Optional[Union[int, str]] = None,
        model_capacity: Optional[Union[int, str]] = None,
        model_fallback: Optional[str] = None,
    ) -> None:
        self.cookies = cookies
        self.language = language
        self.timeout = timeout
        self.token_ttl = token_ttl
        self.on_frame = on_frame  # optional streaming hook

        # Model selection: explicit arguments win over the environment.
        # GEMINI_MODEL=legacy (or "default"/"off") restores the pre-model
        # behaviour exactly; every other value selects a model header.
        if model is None:
            model = os.environ.get("GEMINI_MODEL")
        if thinking is None:
            thinking = os.environ.get("GEMINI_THINKING")
        if model_capacity is None:
            model_capacity = os.environ.get("GEMINI_MODEL_CAPACITY")
        if model_fallback is None:
            model_fallback = os.environ.get("GEMINI_MODEL_FALLBACK")
        if model_fallback is None or not str(model_fallback).strip():
            fallback_names = list(DEFAULT_MODEL_FALLBACK_CHAIN)
        else:
            # GEMINI_MODEL_FALLBACK accepts a comma-separated chain
            # (e.g. "pro,flash-basic"); a single name keeps working.
            fallback_names = [name for name in str(model_fallback).split(",") if name.strip()]

        self.model_setting = resolve_model_setting(model, thinking, model_capacity)
        if self.model_setting is None:
            # Legacy opt-out: one request, model-less payload, exactly as before.
            self.model_chain: list[Optional[ModelSetting]] = [None]
        else:
            chain: list[Optional[ModelSetting]] = [self.model_setting]
            for name in fallback_names:
                setting = resolve_model_setting(name, thinking, model_capacity)
                if setting is not None and setting not in chain:
                    chain.append(setting)
            # Last resort: the legacy payload (web default) still answers.
            chain.append(None)
            self.model_chain = chain

        self.session = requests.Session()
        for name, value in cookies.items():
            try:
                self.session.cookies.set(name, value, domain=".google.com", path="/")
            except Exception:  # noqa: BLE001 - skip undecodable cookie values
                continue
        self.session.headers.update(HEADERS)

        self.access_token: Optional[str] = None
        self.build_label: Optional[str] = None
        self.session_id: Optional[str] = None
        self._token_fetched_at: float = 0.0
        self._last_rotate_at: float = 0.0
        self._reqid = random.randint(10000, 99999)

    # ------------------------------------------------------------------
    # Token bootstrap
    # ------------------------------------------------------------------

    def init(self, force: bool = False) -> None:
        """Fetch SNlM0e / cfb2h / FdrFJe from the web app (cache with TTL).

        When the token cannot be parsed (usually a stale 1PSIDTS), rotate the
        session cookies once and retry before giving up.
        """
        if not force and self.access_token and time.time() - self._token_fetched_at < self.token_ttl:
            return
        try:
            self._fetch_tokens()
        except GeminiWebError:
            if self.rotate_cookies(force=True):
                try:
                    self._fetch_tokens()
                    return
                except GeminiWebError:
                    pass
            raise

    def _fetch_tokens(self) -> None:
        try:
            resp = self.session.get(INIT_URL, timeout=self.timeout)
        except requests.RequestException as exc:
            # Transport failures are surfaced as GeminiWebError so init() can
            # run its existing rotate-and-retry recovery path.
            raise GeminiWebError(
                f"Could not reach {INIT_URL}: {exc}", transport=True
            ) from exc
        if resp.status_code != 200:
            raise GeminiWebError(
                f"Failed to reach {INIT_URL}: HTTP {resp.status_code}. "
                "Cookies may be expired — log in to gemini.google.com in the browser again."
            )
        text = resp.text
        token = re.search(r'"SNlM0e":\s*"(.*?)"', text)
        bl = re.search(r'"cfb2h":\s*"(.*?)"', text)
        sid = re.search(r'"FdrFJe":\s*"(.*?)"', text)
        if not token:
            raise GeminiWebError(
                "Could not parse SNlM0e token from the Gemini page. "
                "Cookies are probably expired; log in to gemini.google.com in the browser."
            )
        self.access_token = token.group(1)
        self.build_label = bl.group(1) if bl else None
        self.session_id = sid.group(1) if sid else None
        self._token_fetched_at = time.time()

    # ------------------------------------------------------------------
    # __Secure-1PSIDTS auto-rotation
    # ------------------------------------------------------------------

    def rotate_cookies(self, force: bool = False) -> bool:
        """Refresh __Secure-1PSIDTS via accounts.google.com/RotateCookies.

        Google issues 1PSIDTS as a short-lived token; the RotateCookies
        endpoint exchanges the current session for a fresh set of cookies
        (including a new 1PSIDTS) via Set-Cookie. Returns True when the
        jar now holds a (possibly new) 1PSIDTS. A 60s cooldown avoids 429s.
        """
        if not force and time.time() - self._last_rotate_at < 60:
            return self.get_cookie("__Secure-1PSIDTS") is not None
        if not self.get_cookie("__Secure-1PSID"):
            return False
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://accounts.google.com",
            "Referer": "https://accounts.google.com/",
        }
        try:
            resp = self.session.post(
                ROTATE_COOKIES_URL,
                headers=headers,
                # Payload mirrored from the web app's cookie-rotation RPC:
                # [000, "-" + <random uint64>]. The 000 prefix is the request
                # id slot; any negative-looking numeric string is accepted.
                data='[000,"-0000000000000000000"]',
                timeout=self.timeout,
            )
            self._last_rotate_at = time.time()
            if resp.status_code == 401 or resp.status_code >= 400:
                return False
            # The server answered with Set-Cookie headers; requests already
            # merged them into the session jar. Verify we still hold a token.
            return self.get_cookie("__Secure-1PSIDTS") is not None
        except requests.RequestException:
            return False

    def get_cookie(self, name: str) -> Optional[str]:
        """Read one cookie from the session jar."""
        for cookie in self.session.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def ensure_fresh(self, force: bool = False) -> None:
        """Rotate the session cookie BEFORE requesting tokens, so the
        short-lived 1PSIDTS is periodically renewed even while idle."""
        if force or time.time() - self._last_rotate_at >= 1500:  # every ~25 min
            self.rotate_cookies()

    def _params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"hl": self.language, "_reqid": self._reqid, "rt": "c"}
        if self.build_label:
            params["bl"] = self.build_label
        if self.session_id:
            params["f.sid"] = self.session_id
        return params

    def _next_reqid(self) -> int:
        rid = self._reqid
        self._reqid += 100000
        return rid

    # ------------------------------------------------------------------
    # Transport resilience (self-healing session + bounded retries)
    # ------------------------------------------------------------------

    def _heal_session(self) -> bool:
        """Recover from a dead session: rotate cookies, then re-init the token.

        Called when StreamGenerate answers 400/401/403 — i.e. the cached SNlM0e
        token (or the short-lived __Secure-1PSIDTS it depends on) expired while
        the client was idle or mid-stream. Expensive but bounded: the caller
        retries exactly once afterwards and never loops back here.
        """
        rotated = self.rotate_cookies(force=True)
        logger.info(
            "session heal: rotate_cookies(force=True) -> %s",
            "rotated" if rotated else "no new 1PSIDTS",
        )
        try:
            self.init(force=True)
        except GeminiWebError as exc:
            logger.warning("session heal: token re-init failed (%s); giving up", exc)
            return False
        logger.info("session heal: token refreshed (session_id=%s)", bool(self.session_id))
        return True

    def _run_with_resilience(self, what: str, operation: Callable[[], Any]) -> Any:
        """Run one HTTP exchange with two bounded recovery layers.

        * HTTP 400/401/403 -> ``_heal_session()`` then retry **once**.
        * 1013 / HTTP 5xx / transport error (timeout, reset, mid-stream
          disconnect) -> up to ``TRANSIENT_RETRY_ATTEMPTS`` retries with the
          ``TRANSIENT_RETRY_DELAYS`` backoff.

        Every attempt is logged; the loop is capped on both branches, so a
        permanently broken endpoint raises ``GeminiWebError`` instead of
        spinning. Errors that are neither stale-session nor transient (usage
        limit, model unavailable, conversation model conflict, ...) propagate
        untouched so the model fallback chain in :meth:`generate` still works.
        """
        healed = False
        retries = 0
        while True:
            try:
                return operation()
            except GeminiWebError as exc:
                if exc.code in SESSION_HEAL_STATUSES and not healed:
                    healed = True
                    logger.warning(
                        "%s: HTTP %s — stale session, healing cookies/token and "
                        "retrying once",
                        what,
                        exc.code,
                    )
                    if not self._heal_session():
                        raise
                    continue
                if exc.is_retryable and retries < TRANSIENT_RETRY_ATTEMPTS:
                    delay = TRANSIENT_RETRY_DELAYS[
                        min(retries, len(TRANSIENT_RETRY_DELAYS) - 1)
                    ]
                    retries += 1
                    logger.warning(
                        "%s: transient failure (%s); retry %d/%d in %.1fs",
                        what,
                        exc,
                        retries,
                        TRANSIENT_RETRY_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise

    # ------------------------------------------------------------------
    # Generate content (StreamGenerate)
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        metadata: Optional[list[Any]] = None,
        files: Optional[list[str]] = None,
        on_frame: Optional[Callable[[Any], None]] = None,
    ) -> GenerationResult:
        """Send one prompt (optionally with attached files) and collect the streamed answer.

        Model and thinking depth come from ``self.model_chain`` (configured via
        the constructor arguments or ``GEMINI_MODEL`` / ``GEMINI_THINKING``).
        When the server rejects a candidate (usage limit, model unavailable) the
        next one is tried — fallback model, then the legacy model-less request —
        so an over-ambitious default degrades instead of breaking the tool.
        """
        self.ensure_fresh()
        self.init()
        start = time.time()

        file_data = None
        if files:
            file_data = [self.upload_file(path) for path in files]

        plans = self.model_chain
        index = 0
        last_error: Optional[GeminiWebError] = None
        while index < len(plans):
            plan = plans[index]
            index += 1
            try:
                result = self._generate_once(prompt, metadata, file_data, plan, on_frame)
            except GeminiWebError as exc:
                last_error = exc
                if exc.code == CONVERSATION_MODEL_CONFLICT:
                    # The conversation already carries another model; only the
                    # legacy (model-less) request can continue it.
                    while index < len(plans) and plans[index] is not None:
                        index += 1
                    if index >= len(plans):
                        raise
                    continue
                if exc.code in MODEL_RETRY_CODES and index < len(plans):
                    continue
                raise
            result.elapsed = time.time() - start
            return result
        raise last_error  # pragma: no cover - plans is never empty

    def _generate_once(
        self,
        prompt: str,
        metadata: Optional[list[Any]],
        file_data: Any,
        plan: Optional[ModelSetting],
        on_frame: Optional[Callable[[Any], None]],
    ) -> GenerationResult:
        """One StreamGenerate round trip for one model plan (None = legacy).

        The request identity (session uuid, model header, inner payload) is
        built once and reused by the resilience layer, so a healed/retried
        attempt stays the same logical turn. Model fallback is *not* handled
        here — see :meth:`generate`.
        """
        session_uuid = str(uuid.uuid4()).upper()
        inner = self._build_inner(prompt, metadata, file_data, plan, session_uuid)
        headers = plan.to_headers(session_uuid) if plan is not None else None
        result = self._run_with_resilience(
            "StreamGenerate",
            lambda: self._stream_once(inner, headers, on_frame),
        )
        logger.debug(
            "StreamGenerate done: %d new frames, %d chars of text, model=%r",
            len(result.raw_frames),
            len(result.text),
            result.model,
        )
        return result

    def _stream_once(
        self,
        inner: list[Any],
        headers: Optional[dict[str, str]],
        on_frame: Optional[Callable[[Any], None]],
    ) -> GenerationResult:
        """One StreamGenerate POST + incremental frame consumption."""
        result = GenerationResult()
        params = {**self._params(), "_reqid": self._next_reqid()}
        body = {
            "at": self.access_token,
            "f.req": json.dumps([None, json.dumps(inner)]),
        }
        request: dict[str, Any] = {
            "params": params,
            "data": body,
            "stream": True,
            "timeout": self.timeout,
        }
        if headers:
            request["headers"] = headers

        parser = FrameParser()
        try:
            with self.session.post(GENERATE_URL, **request) as resp:
                if resp.status_code != 200:
                    raise GeminiWebError(
                        f"StreamGenerate failed: HTTP {resp.status_code}. "
                        "Token may be stale — try again after a page refresh in the browser.",
                        code=resp.status_code,
                    )
                for chunk in resp.iter_content(chunk_size=16384, decode_unicode=True):
                    if not chunk:
                        continue
                    # feed() returns only the frames parsed from *this* chunk;
                    # replaying accumulated history here duplicated media and
                    # on_frame callbacks and inflated raw_frames.
                    for frame in parser.feed(chunk):
                        result.raw_frames.append(frame)
                        if on_frame:
                            on_frame(frame)
                        self._consume_frame(frame, result)
        except requests.RequestException as exc:
            # Mid-stream disconnect / read timeout: the stream is gone, the
            # partial result is discarded and _run_with_resilience retries.
            raise GeminiWebError(
                f"StreamGenerate transport error: {exc}", transport=True
            ) from exc
        return result

    def _build_inner(
        self,
        prompt: str,
        metadata: Optional[list[Any]],
        file_data: Any,
        plan: Optional[ModelSetting],
        session_uuid: str,
    ) -> list[Any]:
        # Legacy requests keep the historical 69-slot shape; model-selected
        # requests grow to 81 slots because inner[79]/inner[80] carry the mode
        # and thinking level.
        inner: list[Any] = [None] * (69 if plan is None else 81)
        inner[0] = [prompt, 0, None, file_data, None, None, 0]
        inner[1] = [self.language]
        inner[2] = metadata or DEFAULT_METADATA.copy()
        inner[6] = [1]
        inner[7] = 1  # streaming flag
        inner[10] = 1
        inner[11] = 0
        inner[17] = [[0]]
        inner[18] = 0
        inner[27] = 1
        inner[30] = [4]
        inner[41] = [1]
        inner[53] = 0
        inner[61] = []
        inner[68] = 2
        inner[59] = session_uuid
        if plan is not None:
            inner[79] = plan.model_number
            inner[80] = plan.thinking
        return inner

    @property
    def model_description(self) -> dict[str, Any]:
        """Diagnostic description of the model chain (used by gemini_status)."""
        if self.model_setting is None:
            return {"model": "legacy", "note": "no model header sent (web app default)"}
        return {
            "model": self.model_setting.label,
            "model_id": self.model_setting.model_id,
            "model_number": self.model_setting.model_number,
            "capacity": self.model_setting.capacity,
            "thinking_level": self.model_setting.thinking,
            "fallbacks": [
                plan.label if plan is not None else "legacy"
                for plan in self.model_chain[1:]
            ],
        }

    def _consume_frame(self, frame: Any, result: GenerationResult) -> None:
        # Error check: frames may carry [.., [5, [2, [0, [1, [0, code]]]]]]-ish paths.
        err_code = get_nested(frame, [5, 2, 0, 1, 0])
        if err_code:
            raise GeminiWebError(
                f"Gemini returned error code {err_code}: {ERROR_CODES.get(err_code, 'unknown')}",
                code=err_code,
            )

        inner_str = get_nested(frame, [2])
        if not isinstance(inner_str, str):
            return
        try:
            inner = json.loads(inner_str)
        except json.JSONDecodeError:
            return

        # chat metadata: [cid, rid, ...]
        m_data = get_nested(inner, [1])
        if isinstance(m_data, list) and m_data:
            cid = m_data[0]
            rid = m_data[1] if len(m_data) > 1 else ""
            if cid:
                result.conversation_id = str(cid)
            if rid:
                result.response_id = str(rid)
            result.metadata = m_data

        # model that produced the answer, e.g. "3.1 Pro" / "3.5 Flash-Lite"
        served_model = get_nested(inner, [42])
        if isinstance(served_model, str) and served_model:
            result.model = served_model

        # final chunk marker (conversation saved)
        ctx = get_nested(inner, [25])
        if isinstance(ctx, str):
            result.completed = True

        # candidates
        for candidate in get_nested(inner, [4], []) or []:
            rcid = get_nested(candidate, [0])
            if not rcid:
                continue
            result.candidate_id = str(rcid)
            text = get_nested(candidate, [1, 0], "")
            if text:
                result.text = text
            thoughts = get_nested(candidate, [37, 0, 0], "")
            if thoughts:
                result.thoughts = thoughts
            if get_nested(candidate, [8, 0]) == 2:
                result.completed = True
            self._consume_media(candidate, result)

    # ------------------------------------------------------------------
    # Media: upload, parse, download
    # ------------------------------------------------------------------

    @staticmethod
    def _add_media(
        result: GenerationResult, kind: str, url: str, title: str = "", alt: str = ""
    ) -> None:
        """Append a media reference, de-duplicated on ``(kind, url)``.

        Streaming frames repeat the same candidate (and thus the same media)
        many times while the answer grows; without this the same image/video
        was appended once per frame. Re-seeing a key now only fills in fields
        that were empty, so order and ``MediaRef`` identity stay stable.
        """
        key = (kind, url)
        for existing in result.media:
            if (existing.kind, existing.url) == key:
                if title and not existing.title:
                    existing.title = title
                if alt and not existing.alt:
                    existing.alt = alt
                return
        result.media.append(MediaRef(kind=kind, url=url, title=title, alt=alt))

    def _consume_media(self, candidate: Any, result: GenerationResult) -> None:
        """Extract web images, generated images and generated videos from a candidate."""
        # Web images cited in the answer
        for img in get_nested(candidate, [12, 1], []) or []:
            url = get_nested(img, [0, 0, 0])
            if url:
                self._add_media(
                    result,
                    "web_image",
                    url,
                    alt=get_nested(img, [0, 4], "") or "",
                )
        # Generated images (plain generation + image-to-image)
        for img in (get_nested(candidate, [12, 7, 0], []) or []) + (
            get_nested(candidate, [12, 0, "8", 0], []) or []
        ):
            url = get_nested(img, [0, 3, 3])
            if url:
                self._add_media(result, "generated_image", url)
        # Generated videos (Veo etc.) — current web app layout:
        # candidate[12][8]["60"][0][0][0][0] = [.., "video.mp4", .., [urls], ..]
        # with urls = [thumbnail, real_download_url, fallback_url].
        for path in ([12, 8, "60", 0, 0, 0, 0, 7], [12, 59, 0, 0, 0, 0, 7]):
            urls = get_nested(candidate, path, [])
            if isinstance(urls, list) and len(urls) >= 2 and urls[1]:
                self._add_media(
                    result,
                    "generated_video",
                    str(urls[1]),
                    title=str(urls[0] or ""),
                )
                break

    def upload_file(self, path: str, filename: Optional[str] = None) -> list[Any]:
        """Upload a local file with the two-phase resumable protocol.

        Phase 1 asks the upload server for a resumable URL, phase 2 streams
        the bytes. Returns the StreamGenerate file entry::

            [[identifier, file_type_int, None, mime_type], filename]

        Transport failures (timeouts, resets) are surfaced as
        :class:`GeminiWebError` with ``transport=True``; ``_run_with_resilience``
        retries them, and a 400/401/403 heals the session first.
        """
        return self._run_with_resilience(
            "upload", lambda: self._upload_file_once(path, filename)
        )

    def _upload_file_once(self, path: str, filename: Optional[str] = None) -> list[Any]:
        """One full two-phase upload attempt (see :meth:`upload_file`)."""
        file_path = Path(path)
        if not file_path.is_file():
            raise GeminiWebError(f"File not found: {path}")
        content = file_path.read_bytes()
        name = filename or file_path.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        file_code = FILES_ENUM_INT.get(
            mime if mime.startswith("application") else mime.split("/")[0], 0
        )

        common = {
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "X-Tenant-Id": "bard-storage",
            "push-id": PUSH_ID,
            "x-goog-upload-protocol": "resumable",
        }

        # Phase 1: start the resumable upload, get the upload URL.
        start_headers = {
            **common,
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(len(content)),
        }
        try:
            resp = self.session.post(
                UPLOAD_URL,
                headers=start_headers,
                data=f"File name: {name}".encode(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise GeminiWebError(
                f"Upload start transport error: {exc}", transport=True
            ) from exc
        if resp.status_code != 200:
            raise GeminiWebError(
                f"Upload start failed: HTTP {resp.status_code} — {resp.text[:200]}",
                code=resp.status_code,
            )
        upload_url = resp.headers.get("x-goog-upload-url")
        if not upload_url:
            raise GeminiWebError(
                f"Upload start returned no x-goog-upload-url: {resp.text[:200]}"
            )

        # Phase 2: upload, finalize.
        upload_headers = {
            **common,
            "x-goog-upload-command": "upload, finalize",
            "x-goog-upload-offset": "0",
        }
        try:
            resp = self.session.post(
                upload_url, headers=upload_headers, data=content, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise GeminiWebError(
                f"Upload finalize transport error: {exc}", transport=True
            ) from exc
        if resp.status_code != 200:
            raise GeminiWebError(
                f"Upload finalize failed: HTTP {resp.status_code} — {resp.text[:200]}",
                code=resp.status_code,
            )
        identifier = resp.text.strip()
        if not identifier.startswith("/"):
            raise GeminiWebError(f"Unexpected upload response: {identifier[:200]}")
        return [[identifier, file_code, None, mime], name]

    def download_media(
        self, url: str, save_path: str, retries: int = 20, retry_delay: float = 10.0
    ) -> str:
        """Download a media URL with the session cookies.

        Generated videos may still be rendering when the URL first appears:
        the server then answers 206/404, and we retry until the content is
        ready (mirrors the web app's polling behavior).
        """
        dest = Path(save_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Referer": f"{BASE_URL}/"}
        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                with self.session.get(
                    url, headers=headers, stream=True, timeout=self.timeout
                ) as resp:
                    if resp.status_code == 200:
                        if not dest.suffix:
                            ctype = (resp.headers.get("content-type") or "").split(";")[0].lower()
                            ext = mimetypes.guess_extension(ctype) or ".bin"
                            dest = dest.with_suffix(ext)
                        with open(dest, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    fh.write(chunk)
                        return str(dest)
                    last_error = GeminiWebError(
                        f"Media download failed: HTTP {resp.status_code} for {url[:120]}",
                        code=resp.status_code,
                    )
            except Exception as e:  # noqa: BLE001 - transient network errors
                last_error = e
            if attempt < retries:
                time.sleep(retry_delay)
        raise last_error or GeminiWebError(f"Media download failed for {url[:120]}")

    # ------------------------------------------------------------------
    # batchexecute RPCs
    # ------------------------------------------------------------------

    def batch_execute(
        self, rpcs: list[tuple[str, str]], source_path: str = "/app"
    ) -> list[Any]:
        """POST one or more RPCs to batchexecute; returns parsed frames.

        Transport failures become ``GeminiWebError(transport=True)`` and go
        through the same bounded retry / session-heal layer as StreamGenerate.
        """
        self.ensure_fresh()
        self.init()
        return self._run_with_resilience(
            "batchexecute", lambda: self._batch_execute_once(rpcs, source_path)
        )

    def _batch_execute_once(
        self, rpcs: list[tuple[str, str]], source_path: str = "/app"
    ) -> list[Any]:
        """One batchexecute round trip (see :meth:`batch_execute`)."""
        params = {
            **self._params(),
            "_reqid": self._next_reqid(),
            "rpcids": ",".join(r[0] for r in rpcs),
            "source-path": source_path,
        }
        body = {
            "at": self.access_token,
            "f.req": json.dumps([[[rpc_id, payload, None, "generic"] for rpc_id, payload in rpcs]]),
        }
        try:
            resp = self.session.post(
                BATCH_EXEC_URL, params=params, data=body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise GeminiWebError(
                f"batchexecute transport error: {exc}", transport=True
            ) from exc
        if resp.status_code != 200:
            raise GeminiWebError(f"batchexecute failed: HTTP {resp.status_code}", code=resp.status_code)
        return _parse_plain_response(resp.text)

    def list_conversations(self, limit: int = 13) -> list[dict[str, Any]]:
        """List recent conversations (both pinned and unpinned buckets)."""
        chats: dict[str, dict[str, Any]] = {}
        for pinned_flag in ([1, None, 1], [0, None, 1]):
            frames = self.batch_execute([(GRPC_LIST_CHATS, json.dumps([limit, None, pinned_flag]))])
            for frame in frames:
                body_str = get_nested(frame, [2])
                if not isinstance(body_str, str):
                    continue
                try:
                    body = json.loads(body_str)
                except json.JSONDecodeError:
                    continue
                for conv in get_nested(body, [2], []) or []:
                    if not (isinstance(conv, list) and len(conv) > 1 and conv[0]):
                        continue
                    cid = str(conv[0])
                    ts = get_nested(conv, [5], [])
                    timestamp = 0.0
                    if isinstance(ts, list) and len(ts) >= 2 and isinstance(ts[0], (int, float)):
                        timestamp = float(ts[0]) + (float(ts[1]) / 1e9 if ts[1] else 0)
                    chats[cid] = {
                        "conversation_id": cid,
                        "title": str(conv[1] or ""),
                        "pinned": bool(get_nested(conv, [2])),
                        "timestamp": timestamp,
                    }
        return sorted(chats.values(), key=lambda c: c["timestamp"], reverse=True)

    def read_conversation(self, conversation_id: str, limit: int = 10) -> dict[str, Any]:
        """Read a conversation's turns (newest first)."""
        frames = self.batch_execute(
            [(GRPC_READ_CHAT, json.dumps([conversation_id, limit, None, 1, [1], [4], None, 1]))]
        )
        turns: list[dict[str, Any]] = []
        for frame in frames:
            body_str = get_nested(frame, [2])
            if not isinstance(body_str, str):
                continue
            try:
                body = json.loads(body_str)
            except json.JSONDecodeError:
                continue
            for turn in get_nested(body, [0], []) or []:
                if not isinstance(turn, list):
                    continue
                rid = get_nested(turn, [0, 1], "")
                user_text = get_nested(turn, [2, 0, 0], "")
                if user_text:
                    turns.append({"role": "user", "text": str(user_text)})
                for candidate in get_nested(turn, [3, 0], []) or []:
                    rcid = get_nested(candidate, [0])
                    text = get_nested(candidate, [1, 0], "")
                    status = get_nested(candidate, [8, 0])
                    if rcid and text:
                        entry: dict[str, Any] = {
                            "role": "model",
                            "text": str(text),
                            "response_id": str(rid),
                            "candidate_id": str(rcid),
                            "status": status,
                            "media": [],
                        }
                        media_probe = GenerationResult()
                        self._consume_media(candidate, media_probe)
                        entry["media"] = [m.to_dict() for m in media_probe.media]
                        turns.append(entry)
        return {"conversation_id": conversation_id, "turns": turns}

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation (two RPC calls, same as the web UI)."""
        self.batch_execute([(GRPC_DELETE_CHAT_1, json.dumps([conversation_id]))])
        self.batch_execute([(GRPC_DELETE_CHAT_2, json.dumps([conversation_id, [1, None, 0, 1]]))])


def _parse_plain_response(text: str) -> list[Any]:
    """Parse a non-streaming batchexecute response into a flat frame list."""
    content = text
    if content.startswith(")]}'"):
        content = content[4:]
    content = content.lstrip()
    parser = FrameParser()
    # feed() returns only what this call parsed — the whole body goes in at
    # once, so that is exactly the frame list (self.frames is a debug log).
    frames = parser.feed(content)
    if frames:
        return frames
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, list) else [obj]
    except json.JSONDecodeError:
        return []
