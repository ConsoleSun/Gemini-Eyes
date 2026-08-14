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
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

import requests

BASE_URL = "https://gemini.google.com"
INIT_URL = f"{BASE_URL}/app"
GENERATE_URL = (
    f"{BASE_URL}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
)
BATCH_EXEC_URL = f"{BASE_URL}/_/BardChatUi/data/batchexecute"

# Default 10-slot metadata used for brand-new conversations (mirrors the web app).
DEFAULT_METADATA: list[Any] = ["", "", "", None, None, None, None, None, None, ""]

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
    """Raised for non-200 responses and known server error codes."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


@dataclass
class GenerationResult:
    text: str = ""
    thoughts: str = ""
    conversation_id: str = ""
    response_id: str = ""
    candidate_id: str = ""
    completed: bool = False
    metadata: list[Any] = field(default_factory=list)
    raw_frames: list[Any] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "thoughts": self.thoughts,
            "conversation_id": self.conversation_id,
            "response_id": self.response_id,
            "candidate_id": self.candidate_id,
            "completed": self.completed,
            "elapsed_seconds": round(self.elapsed, 2),
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
        self._buf += chunk
        # Strip the anti-XSSI prefix once (">]}'") plus surrounding whitespace.
        if self._buf.startswith(")]}'"):
            self._buf = self._buf[4:].lstrip()
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
            start = pos + m.end()
            # Count UTF-16 units of the remaining buffer to know if the frame
            # payload has fully arrived.
            units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in self._buf[start:])
            if units < length:
                break
            # Advance by `length` UTF-16 units.
            end = start
            remaining = length
            while remaining > 0 and end < len(self._buf):
                remaining -= 2 if ord(self._buf[end]) > 0xFFFF else 1
                end += 1
            payload = self._buf[start:end].strip()
            pos = end
            if payload:
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, list):
                    self.frames.extend(obj)
                else:
                    self.frames.append(obj)
        self._buf = self._buf[pos:]
        return self.frames


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
    ) -> None:
        self.cookies = cookies
        self.language = language
        self.timeout = timeout
        self.token_ttl = token_ttl
        self.on_frame = on_frame  # optional streaming hook

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
        self._reqid = random.randint(10000, 99999)

    # ------------------------------------------------------------------
    # Token bootstrap
    # ------------------------------------------------------------------

    def init(self, force: bool = False) -> None:
        """Fetch SNlM0e / cfb2h / FdrFJe from the web app (cache with TTL)."""
        if not force and self.access_token and time.time() - self._token_fetched_at < self.token_ttl:
            return
        resp = self.session.get(INIT_URL, timeout=self.timeout)
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
    # Generate content (StreamGenerate)
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        metadata: Optional[list[Any]] = None,
        on_frame: Optional[Callable[[Any], None]] = None,
    ) -> GenerationResult:
        """Send one prompt and collect the streamed answer."""
        self.init()
        start = time.time()
        result = GenerationResult()

        inner: list[Any] = [None] * 69
        inner[0] = [prompt, 0, None, None, None, None, 0]
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
        inner[59] = str(uuid.uuid4()).upper()

        params = {**self._params(), "_reqid": self._next_reqid()}
        body = {
            "at": self.access_token,
            "f.req": json.dumps([None, json.dumps(inner)]),
        }

        parser = FrameParser()
        with self.session.post(
            GENERATE_URL, params=params, data=body, stream=True, timeout=self.timeout
        ) as resp:
            if resp.status_code != 200:
                raise GeminiWebError(
                    f"StreamGenerate failed: HTTP {resp.status_code}. "
                    "Token may be stale — try again after a page refresh in the browser.",
                    code=resp.status_code,
                )
            for chunk in resp.iter_content(chunk_size=16384, decode_unicode=True):
                if not chunk:
                    continue
                for frame in parser.feed(chunk):
                    result.raw_frames.append(frame)
                    if on_frame:
                        on_frame(frame)
                    self._consume_frame(frame, result)

        result.elapsed = time.time() - start
        return result

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

    # ------------------------------------------------------------------
    # batchexecute RPCs
    # ------------------------------------------------------------------

    def batch_execute(
        self, rpcs: list[tuple[str, str]], source_path: str = "/app"
    ) -> list[Any]:
        """POST one or more RPCs to batchexecute; returns parsed frames."""
        self.init()
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
        resp = self.session.post(BATCH_EXEC_URL, params=params, data=body, timeout=self.timeout)
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
                        turns.append(
                            {
                                "role": "model",
                                "text": str(text),
                                "response_id": str(rid),
                                "candidate_id": str(rcid),
                                "status": status,
                            }
                        )
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
    parser.feed(content)
    if parser.frames:
        return parser.frames
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, list) else [obj]
    except json.JSONDecodeError:
        return []
