"""Tests for the Gemini web RPC client: frame parser, response consumption,
request construction and conversation RPCs (no network needed)."""

from __future__ import annotations

import json

import pytest

from gemini_mcp.gemini_client import (
    DEFAULT_METADATA,
    FrameParser,
    GeminiWebClient,
    GeminiWebError,
    GenerationResult,
    _parse_plain_response,
    get_nested,
)


# ---------------------------------------------------------------------------
# get_nested
# ---------------------------------------------------------------------------

def test_get_nested_paths():
    data = {"a": [{"b": [1, 2, {"c": "x"}]}]}
    assert get_nested(data, ["a", 0, "b", 2, "c"]) == "x"
    assert get_nested(data, ["a", 0, "b", 9]) is None
    assert get_nested(data, ["missing"], "dflt") == "dflt"
    assert get_nested(data, ["a", 0, "b", 0]) == 1


# ---------------------------------------------------------------------------
# FrameParser (length-prefixed streaming frames)
# ---------------------------------------------------------------------------

def _frame(payload) -> str:
    s = json.dumps(payload, ensure_ascii=False)
    return f"{len(s)}\n{s}\n"


def test_frame_parser_single_and_multiple():
    p = FrameParser()
    p.feed(_frame([["wrb.fr", None, "{}", None, None, None, "generic"]]) * 2)
    assert len(p.frames) == 2
    assert p.frames[0][0] == "wrb.fr"


def test_frame_parser_chunk_boundaries():
    raw = _frame([["wrb.fr", None, "{\"a\":1}", None]])
    p = FrameParser()
    for i in range(0, len(raw), 3):  # split mid-frame
        p.feed(raw[i : i + 3])
    assert len(p.frames) == 1
    assert p.frames[0][2] == '{"a":1}'


def test_frame_parser_utf16_length():
    # JSON containing a 4-byte emoji: JS String.length counts it as 2 units.
    payload = [["wrb.fr", None, "你好👋", None]]
    s = json.dumps(payload, ensure_ascii=False)
    units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)
    raw = f"{units}\n{s}\n"
    p = FrameParser()
    p.feed(raw)
    assert len(p.frames) == 1
    assert p.frames[0][2] == "你好👋"


def test_frame_parser_partial_then_rest():
    p = FrameParser()
    raw = _frame([[1, 2, 3]])
    p.feed(raw[:5])
    assert p.frames == []
    p.feed(raw[5:])
    assert p.frames == [[1, 2, 3]]


# ---------------------------------------------------------------------------
# Response consumption (_consume_frame)
# ---------------------------------------------------------------------------

def _stream_frame(cid="c_abc", rid="r_123", rcid="rc_456", text="hi there",
                  status=1, thoughts="", error_code=None):
    inner: list = [None] * 30
    inner[1] = [cid, rid]
    inner[4] = [[rcid, [text], None, None, None, None, None, None, [status], None,
                 None, None, None, None, None, None, None, None, None, None, None,
                 None, None, None, None, None, None, None, None, None, None, None,
                 None, None, None, None, None, thoughts and [[thoughts]] or None]]
    frame = ["wrb.fr", None, json.dumps(inner), None, None, None, "generic"]
    if error_code is not None:
        # Error frames carry the code at part[5][2][0][1][0].
        frame[5] = [None, None, [[None, [error_code]]]]
    return frame


def test_consume_frame_extracts_result():
    client = GeminiWebClient({})
    result = GenerationResult()
    client._consume_frame(_stream_frame(), result)
    assert result.conversation_id == "c_abc"
    assert result.response_id == "r_123"
    assert result.candidate_id == "rc_456"
    assert result.text == "hi there"


def test_consume_frame_final_status_and_thoughts():
    client = GeminiWebClient({})
    result = GenerationResult()
    client._consume_frame(
        _stream_frame(text="full answer", status=2, thoughts="let me think"), result
    )
    assert result.text == "full answer"
    assert result.thoughts == "let me think"
    assert result.completed is True


def test_consume_frame_error_code_raises():
    client = GeminiWebClient({})
    result = GenerationResult()
    with pytest.raises(GeminiWebError) as exc:
        client._consume_frame(_stream_frame(error_code=1037), result)
    assert exc.value.code == 1037
    assert "usage limit" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# generate() request shape & streaming integration (mocked transport)
# ---------------------------------------------------------------------------

class FakeResponse:
    status_code = 200

    def __init__(self, frames):
        self._frames = frames

    def iter_content(self, chunk_size=0, decode_unicode=False):
        raw = ")]}'\n\n" + "".join(
            f"{len(json.dumps([f]))}\n{json.dumps([f])}\n" for f in self._frames
        )
        for i in range(0, len(raw), 64):
            yield raw[i : i + 64]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_generate_uses_browser_shaped_request(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.cookies = _FakeCookieJar()
            self.headers = {}

        def get(self, url, **kw):
            captured["init_url"] = url
            return _FakeInitResponse()

        def post(self, url, params=None, data=None, stream=None, timeout=None):
            captured["post_url"] = url
            captured["params"] = params
            captured["data"] = data
            return FakeResponse([
                _stream_frame(cid="c_1", rid="r_1", rcid="rc_1", text="answer!", status=2),
            ])

    class _FakeCookieJar:
        def set(self, *a, **k):
            pass

    class _FakeInitResponse:
        status_code = 200
        text = 'window.WIZ_global_data = {"SNlM0e":"tok123","cfb2h":"bl123","FdrFJe":"sid123"}'

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({"__Secure-1PSID": "v"})
    res = client.generate("ping")

    assert captured["init_url"].endswith("/app")
    assert captured["post_url"].endswith("/StreamGenerate")
    assert captured["params"]["rt"] == "c"
    assert captured["params"]["hl"] == "zh-CN"
    assert captured["data"]["at"] == "tok123"

    f_req = json.loads(captured["data"]["f.req"])
    inner = json.loads(f_req[1])
    assert inner[0][0] == "ping"
    assert inner[2] == DEFAULT_METADATA  # brand-new chat metadata
    assert inner[7] == 1  # streaming flag

    assert res.text == "answer!"
    assert res.conversation_id == "c_1"
    assert res.response_id == "r_1"
    assert res.completed is True


def test_generate_continues_conversation(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {
                "status_code": 200,
                "text": '{"SNlM0e":"tok123"}',
            })()

        def post(self, url, params=None, data=None, stream=None, timeout=None):
            captured["data"] = data
            return FakeResponse([_stream_frame(cid="c_9", rid="r_9", text="ok", status=2)])

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    client.generate("again", metadata=["c_9", "r_8", "", None, None, None, None, None, None, ""])
    inner = json.loads(json.loads(captured["data"]["f.req"])[1])
    assert inner[2][0] == "c_9"
    assert inner[2][1] == "r_8"


def test_generate_non_200_raises(monkeypatch):
    class BadResponse:
        status_code = 400
        text = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, **kw):
            return BadResponse()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    with pytest.raises(GeminiWebError):
        client.generate("x")


# ---------------------------------------------------------------------------
# batchexecute + conversation RPCs
# ---------------------------------------------------------------------------

def _batch_response(frames):
    return ")]}'\n\n" + "".join(
        f"{len(json.dumps([f]))}\n{json.dumps([f])}\n" for f in frames
    )


def test_parse_plain_response():
    frames = _parse_plain_response(_batch_response([[None, "rpc", "[2]", None]]))
    assert frames == [[None, "rpc", "[2]", None]]

def test_list_conversations_parses(monkeypatch):
    conv = ["c_42", "My Chat", True, None, None, [1730000000, 500000000], None]
    inner = json.dumps([None, None, [conv]])

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, params=None, data=None, **kw):
            assert params["rpcids"] == "MaZiqc"
            return type("R", (), {
                "status_code": 200,
                "text": _batch_response([["wrb.fr", None, inner, None, None, None, "generic"]]),
            })()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    chats = client.list_conversations(limit=5)
    assert len(chats) == 1
    assert chats[0]["conversation_id"] == "c_42"
    assert chats[0]["title"] == "My Chat"
    assert chats[0]["pinned"] is True
    assert chats[0]["timestamp"] == pytest.approx(1730000000.5)


def test_delete_conversation_uses_both_rpcs(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, params=None, data=None, **kw):
            calls.append(params["rpcids"])
            return type("R", (), {"status_code": 200, "text": ")]}'\n\n"})()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    client.delete_conversation("c_1")
    assert calls == ["GzXR5e", "qWymEb"]


def test_init_requires_snlm0e():
    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": "<html>no token here</html>"})()

    import gemini_mcp.gemini_client as gc
    original = gc.requests.Session
    gc.requests.Session = FakeSession
    try:
        client = GeminiWebClient({})
        with pytest.raises(GeminiWebError, match="SNlM0e"):
            client.init(force=True)
    finally:
        gc.requests.Session = original
