"""Tests for the Gemini web RPC client: frame parser, response consumption,
request construction and conversation RPCs (no network needed)."""

from __future__ import annotations

import json
import time
from pathlib import Path

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
    # Google's length marker includes the newline after the digits and the
    # newline after the JSON payload: marker = 1 + len(json) + 1.
    return f"{len(s) + 2}\n\n{s}\n"


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
    raw = f"{units + 2}\n\n{s}\n"  # marker includes both newlines
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
                  status=1, thoughts="", error_code=None, model=""):
    inner: list = [None] * 43
    inner[1] = [cid, rid]
    inner[4] = [[rcid, [text], None, None, None, None, None, None, [status], None,
                 None, None, None, None, None, None, None, None, None, None, None,
                 None, None, None, None, None, None, None, None, None, None, None,
                 None, None, None, None, None, thoughts and [[thoughts]] or None]]
    if model:
        inner[42] = model  # response slot carrying the serving model name
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
            f"{len(json.dumps([f])) + 2}\n\n{json.dumps([f])}\n" for f in self._frames
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

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            captured["post_url"] = url
            captured["params"] = params
            captured["data"] = data
            captured["headers"] = headers
            return FakeResponse([
                _stream_frame(cid="c_1", rid="r_1", rcid="rc_1", text="answer!", status=2),
            ])

    class _FakeCookieJar:
        def set(self, *a, **k):
            pass

        def __iter__(self):
            return iter(())

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
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {
                "status_code": 200,
                "text": '{"SNlM0e":"tok123"}',
            })()

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            captured["data"] = data
            captured["headers"] = headers
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
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
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
        f"{len(json.dumps([f])) + 2}\n\n{json.dumps([f])}\n" for f in frames
    )


def test_parse_plain_response():
    frames = _parse_plain_response(_batch_response([[None, "rpc", "[2]", None]]))
    assert frames == [[None, "rpc", "[2]", None]]

def test_list_conversations_parses(monkeypatch):
    conv = ["c_42", "My Chat", True, None, None, [1730000000, 500000000], None]
    inner = json.dumps([None, None, [conv]])

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
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
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
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
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
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


# ---------------------------------------------------------------------------
# Media: upload, generate-with-files, media parsing, download
# ---------------------------------------------------------------------------

def test_upload_file_two_phase_resumable(monkeypatch, tmp_path):
    captured = []
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, headers=None, data=None, **kw):
            captured.append((url, headers, data))
            if url == "https://push.clients6.google.com/upload":
                return type("R", (), {
                    "status_code": 200,
                    "headers": {"x-goog-upload-url": "https://upload.example/up"},
                    "text": "",
                })()
            return type("R", (), {"status_code": 200, "text": "/contrib_service/ttl_1d/12345"})()

    import gemini_mcp.gemini_client as gc
    monkeypatch.setattr(gc.requests, "Session", FakeSession)
    client = GeminiWebClient({})
    entry = client.upload_file(str(img))
    assert len(captured) == 2
    url1, h1, d1 = captured[0]
    assert url1 == "https://push.clients6.google.com/upload"
    assert h1["x-goog-upload-command"] == "start"
    assert h1["x-goog-upload-header-content-length"] == "19"
    assert h1["push-id"] == "feeds/mcudyrk2a4khkz"
    assert d1 == b"File name: photo.png"
    url2, h2, d2 = captured[1]
    assert url2 == "https://upload.example/up"
    assert h2["x-goog-upload-command"] == "upload, finalize"
    assert h2["x-goog-upload-offset"] == "0"
    assert d2 == b"\x89PNG\r\n\x1a\nfakepngdata"
    # file entry: [[identifier, type_int, None, mime], filename]
    assert entry == [["/contrib_service/ttl_1d/12345", 1, None, "image/png"], "photo.png"]


def test_upload_file_missing_path_raises():
    client = GeminiWebClient({})
    with pytest.raises(GeminiWebError):
        client.upload_file("/nonexistent/file.png")


def test_generate_with_files_embeds_file_data(monkeypatch, tmp_path):
    captured = {}
    img = tmp_path / "pic.png"
    img.write_bytes(b"pngdata")

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"tok"}'})()

        def post(self, url, params=None, data=None, stream=None, timeout=None, **kw):
            if "clients6.google.com/upload" in url:
                return type("R", (), {
                    "status_code": 200,
                    "headers": {"x-goog-upload-url": "https://upload.example/up"},
                    "text": "",
                })()
            if url == "https://upload.example/up":
                return type("R", (), {"status_code": 200, "text": "/contrib_service/x"})()

            captured["data"] = data
            return FakeResponse(
                [_stream_frame(cid="c_1", rid="r_1", text="I see an image.", status=2)]
            )

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    res = client.generate("what is this?", files=[str(img)])
    assert res.text == "I see an image."
    inner = json.loads(json.loads(captured["data"]["f.req"])[1])
    assert inner[0][3] == [[["/contrib_service/x", 1, None, "image/png"], "pic.png"]]


def test_consume_media_generated_image_and_video():
    client = GeminiWebClient({})
    result = GenerationResult()
    # candidate with a generated image at [12][7][0] and a generated video at [12][59]
    candidate = [None] * 40
    candidate[0] = "rc_x"
    candidate[1] = ["here is your image"]
    candidate[8] = [2]
    candidate[12] = [None] * 90
    # generated image: [12][7][0] = list of gen_img_data, url = gen_img_data[0][3][3]
    gen_img_data = [[None, None, None, [None, None, None,
                     "http://googleusercontent.com/gen/1.png"]], "img_id"]
    candidate[12][7] = [[gen_img_data]]
    # generated video: [12][8]["60"][0][0][0][0][7] = [thumb, url, fallback]
    candidate[12][8] = {"60": [[[[[None, 2, "video.mp4", None, None, None, None,
                                   ["http://thumb.example/t.jpg",
                                    "http://video.example/v.mp4",
                                    "http://fallback.example/f.mp4"]]]]]]}
    client._consume_frame(
        ["wrb.fr", None, json.dumps([None, None, None, None, [candidate]])], result
    )
    kinds = [m.kind for m in result.media]
    assert "generated_image" in kinds
    assert "generated_video" in kinds
    img = next(m for m in result.media if m.kind == "generated_image")
    assert img.url == "http://googleusercontent.com/gen/1.png"
    vid = next(m for m in result.media if m.kind == "generated_video")
    assert vid.url == "http://video.example/v.mp4"


def test_download_media_saves_file(monkeypatch, tmp_path):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None, "__iter__": lambda self: iter(())})()
            self.headers = {}

        class MediaResponse:
            status_code = 200
            headers = {"content-type": "image/png"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, chunk_size=0):
                return iter([b"\x89PNG-data"])

        def get(self, url, **kw):
            if "gemini.google.com" in url:
                return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()
            captured["url"] = url
            return self.MediaResponse()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    dest = client.download_media("http://googleusercontent.com/img.png", str(tmp_path / "out"))
    assert captured["url"] == "http://googleusercontent.com/img.png"
    assert Path(dest).read_bytes() == b"\x89PNG-data"
    assert dest.endswith(".png")  # extension inferred from content-type


# ---------------------------------------------------------------------------
# Cookie rotation (RotateCookies)
# ---------------------------------------------------------------------------

def test_rotate_cookies_posts_and_updates_jar(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.cookies = type(
                "J",
                (),
                {
                    "set": lambda *a, **k: None,
                    "__iter__": lambda self: iter([
                        type("C", (), {"name": "__Secure-1PSID", "value": "psid-1",
                                        "domain": ".google.com", "path": "/"}),
                        type("C", (), {"name": "__Secure-1PSIDTS", "value": "ts-1",
                                        "domain": ".google.com", "path": "/"}),
                    ]),
                },
            )()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, headers=None, data=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return type("R", (), {"status_code": 200, "text": ""})()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    # __Secure-1PSID present -> rotation proceeds
    client.session.cookies = FakeSession().cookies
    ok = client.rotate_cookies(force=True)
    assert ok is True
    assert captured["url"] == "https://accounts.google.com/RotateCookies"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["Origin"] == "https://accounts.google.com"
    assert captured["data"] == '[000,"-0000000000000000000"]'


def test_rotate_cookies_skips_without_1psid(monkeypatch):
    class FakeSession:
        def __init__(self):
            self.cookies = type(
                "J", (), {"__iter__": lambda self: iter([])}
            )()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, **kw):
            raise AssertionError("should not POST without __Secure-1PSID")

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    client.session.cookies = FakeSession().cookies
    assert client.rotate_cookies(force=True) is False


def test_rotate_cookies_401_returns_false(monkeypatch):
    class FakeSession:
        def __init__(self):
            self.cookies = type(
                "J",
                (),
                {
                    "__iter__": lambda self: iter([
                        type("C", (), {"name": "__Secure-1PSID", "value": "x",
                                        "domain": ".google.com", "path": "/"}),
                    ]),
                },
            )()
            self.headers = {}

        def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"t"}'})()

        def post(self, url, **kw):
            return type("R", (), {"status_code": 401, "text": ""})()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    client.session.cookies = FakeSession().cookies
    assert client.rotate_cookies(force=True) is False


def test_init_retries_after_rotation(monkeypatch):
    """init() failing to parse SNlM0e should rotate once and retry."""
    calls = {"get": 0}

    class FakeSession:
        def __init__(self):
            self.cookies = type(
                "J",
                (),
                {
                    "set": lambda *a, **k: None,
                    "__iter__": lambda self: iter([
                        type("C", (), {"name": "__Secure-1PSID", "value": "x",
                                        "domain": ".google.com", "path": "/"}),
                        type("C", (), {"name": "__Secure-1PSIDTS", "value": "y",
                                        "domain": ".google.com", "path": "/"}),
                    ]),
                },
            )()
            self.headers = {}

        def get(self, url, **kw):
            calls["get"] += 1
            if calls["get"] == 1:
                return type("R", (), {"status_code": 200, "text": "<html>no token</html>"})()
            return type("R", (), {"status_code": 200, "text": '{"SNlM0e":"tok-ok"}'})()

        def post(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": ""})()

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    client.session.cookies = FakeSession().cookies
    client.init(force=True)
    assert calls["get"] == 2
    assert client.access_token == "tok-ok"


def test_ensure_fresh_rotates_after_interval(monkeypatch):
    client = GeminiWebClient({})
    client._last_rotate_at = time.time() - 2000  # older than 1500s threshold
    rotated = []
    client.rotate_cookies = lambda force=False: rotated.append(force) or True
    client.ensure_fresh()
    assert rotated == [False]
    # fresh -> no rotation
    client._last_rotate_at = time.time()
    client.ensure_fresh()
    assert len(rotated) == 1


def test_persist_cookies_writes_file(monkeypatch, tmp_path):
    import gemini_mcp.server as server

    cookie_file = tmp_path / "cookies.json"
    monkeypatch.setattr(server, "_cookie_file", str(cookie_file))

    client = GeminiWebClient({})
    client.session.cookies.set("__Secure-1PSID", "v1", domain=".google.com", path="/")
    client.session.cookies.set("NID", "v2", domain=".google.com", path="/")
    client.session.cookies.set("other", "v3", domain="example.org", path="/")

    server._persist_cookies(client)
    raw = json.loads(cookie_file.read_text(encoding="utf-8"))
    names = {c["name"] for c in raw}
    assert names == {"__Secure-1PSID", "NID"}  # google cookies only
    assert (cookie_file.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Model selection (side-channel header + payload slots) and fallbacks
# ---------------------------------------------------------------------------

from gemini_mcp.gemini_client import (  # noqa: E402
    MODEL_CATALOG,
    MODEL_HEADER_KEY,
    resolve_model_setting,
    resolve_thinking_level,
)


class _ModelFakeInit:
    status_code = 200
    text = 'window.WIZ_global_data = {"SNlM0e":"tok","cfb2h":"bl","FdrFJe":"sid"}'


def _model_client(monkeypatch, responses, captured, **kwargs):
    """Client whose fake transport replays ``responses`` (frames per POST)."""

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None,
                                          "__iter__": lambda self: iter(())})()
            self.headers = {}

        def get(self, url, **kw):
            return _ModelFakeInit()

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            captured.append({"params": params, "data": data, "headers": headers})
            return FakeResponse(responses[len(captured) - 1])

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    return GeminiWebClient({}, **kwargs)


def _inner_of(capture):
    return json.loads(json.loads(capture["data"]["f.req"])[1])


def test_resolve_model_setting_names_aliases_and_legacy():
    assert resolve_model_setting("argon").model_number == 8
    assert resolve_model_setting("gemini-4").label == "argon"
    assert resolve_model_setting("4-argon").label == "argon"
    assert resolve_model_setting("pro").model_number == 3
    assert resolve_model_setting("gemini-3.1-pro").label == "pro"
    assert resolve_model_setting("PRO").label == "pro"
    assert resolve_model_setting("highest").label == "argon"  # alias -> top tier
    assert resolve_model_setting("flash").model_number == 1
    assert resolve_model_setting("unknown-model").label == "argon"  # safe default
    assert resolve_model_setting(None).label == "argon"  # highest by default
    assert resolve_model_setting("legacy") is None
    assert resolve_model_setting("off") is None
    # capacity comes from the catalog, overridable
    assert resolve_model_setting("pro").capacity == MODEL_CATALOG["pro"][1]
    assert resolve_model_setting("argon").capacity == MODEL_CATALOG["argon"][1]
    assert resolve_model_setting("pro", capacity=2).capacity == 2
    assert resolve_model_setting("pro", capacity="bogus").capacity == 4


def test_resolve_thinking_level():
    assert resolve_thinking_level(1) == 1
    assert resolve_thinking_level("standard") == 1
    assert resolve_thinking_level("high") == 2
    assert resolve_thinking_level("deep") == 3
    assert resolve_thinking_level(None) == 2  # highest synchronous depth
    assert resolve_thinking_level(9) == 2  # out-of-range -> default
    assert resolve_thinking_level("nonsense") == 2


def test_generate_sends_model_header_and_payload_slots(monkeypatch):
    captured = []
    client = _model_client(
        monkeypatch,
        [[_stream_frame(text="我能用", model="3.1 Pro", status=2)]],
        captured,
        model="pro",
        thinking=2,
    )
    res = client.generate("ping")
    assert len(captured) == 1
    inner = _inner_of(captured[0])
    assert len(inner) == 81
    assert inner[79] == 3  # model number (MODE_CATEGORY: Pro)
    assert inner[80] == 2  # thinking level (extended)
    header = json.loads(captured[0]["headers"][MODEL_HEADER_KEY])
    assert header[4] == MODEL_CATALOG["pro"][0]  # model id hash
    assert header[11] == MODEL_CATALOG["pro"][1]  # capacity
    assert header[14] == 3  # model number
    assert header[15] == 2  # thinking level
    assert header[16] == inner[59]  # request uuid
    assert res.model == "3.1 Pro"


def test_generate_legacy_model_keeps_old_request_shape(monkeypatch):
    captured = []
    client = _model_client(monkeypatch, [[_stream_frame(text="ok", status=2)]], captured,
                           model="legacy")
    client.generate("ping")
    inner = _inner_of(captured[0])
    assert len(inner) == 69  # historical shape, no model slots
    assert inner[79:] == [] or all(v is None for v in inner[79:])
    assert captured[0]["headers"] is None  # no side-channel headers
    assert client.model_description["model"] == "legacy"


def test_generate_falls_back_when_model_rejected(monkeypatch):
    captured = []
    client = _model_client(
        monkeypatch,
        [
            [_stream_frame(error_code=1052)],  # Argon header unavailable
            [_stream_frame(error_code=1052)],  # 3.1 Pro header unavailable too
            [_stream_frame(text="fallback answer", model="3.8 Flash", status=2)],
        ],
        captured,
    )
    res = client.generate("ping")
    assert res.text == "fallback answer"
    assert len(captured) == 3
    # Default chain: Argon -> 3.1 Pro -> Flash-Basic -> legacy model-less.
    first_header = json.loads(captured[0]["headers"][MODEL_HEADER_KEY])
    second_header = json.loads(captured[1]["headers"][MODEL_HEADER_KEY])
    fallback_header = json.loads(captured[2]["headers"][MODEL_HEADER_KEY])
    assert first_header[14] == 8  # Argon
    assert first_header[4] == MODEL_CATALOG["argon"][0]
    assert second_header[14] == 3  # 3.1 Pro
    assert second_header[4] == MODEL_CATALOG["pro"][0]
    assert fallback_header[4] == MODEL_CATALOG["flash-basic"][0]
    assert fallback_header[14] == 1


def test_generate_conversation_model_conflict_retries_legacy(monkeypatch):
    captured = []
    client = _model_client(
        monkeypatch,
        [
            [_stream_frame(error_code=1050)],  # model inconsistent with history
            [_stream_frame(text="legacy answer", status=2)],
        ],
        captured,
    )
    res = client.generate("ping", metadata=["c_1", "r_1", "", None, None, None,
                                            None, None, None, ""])
    assert res.text == "legacy answer"
    assert len(captured) == 2
    assert captured[1]["headers"] is None
    assert len(_inner_of(captured[1])) == 69


def test_environment_configures_model(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "flash")
    monkeypatch.setenv("GEMINI_THINKING", "standard")
    monkeypatch.setenv("GEMINI_MODEL_CAPACITY", "1")
    client = GeminiWebClient({})
    assert client.model_setting.label == "flash"
    assert client.model_setting.thinking == 1
    assert client.model_setting.capacity == 1
    assert client.model_chain[1] is not None  # fallback model
    assert client.model_chain[-1] is None  # legacy last resort
    desc = client.model_description
    assert desc["model"] == "flash"
    assert desc["thinking_level"] == 1


def test_default_model_is_argon_with_degrade_chain(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_FALLBACK", raising=False)
    client = GeminiWebClient({})
    assert client.model_description["model"] == "argon"
    assert [p.label if p is not None else None for p in client.model_chain] == [
        "argon",
        "pro",
        "flash-basic",
        None,
    ]
    assert client.model_setting.model_number == 8
    assert client.model_setting.thinking == 2


def test_fallback_env_accepts_comma_separated_chain(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "pro")
    monkeypatch.setenv("GEMINI_MODEL_FALLBACK", "pro,flash-lite,legacy")
    client = GeminiWebClient({})
    assert [p.label if p is not None else None for p in client.model_chain] == [
        "pro",
        "flash-lite",
        None,
    ]


# ---------------------------------------------------------------------------
# Regression: frame duplication (feed() must return only *new* frames)
# ---------------------------------------------------------------------------

def test_feed_returns_only_frames_parsed_from_this_chunk():
    p = FrameParser()
    first = p.feed(_frame([["a"]]))
    assert first == [["a"]]
    # Second chunk: the old implementation returned *both* frames here.
    second = p.feed(_frame([["b"]]))
    assert second == [["b"]]
    # self.frames stays as the debug accumulator (unchanged public field).
    assert p.frames == [["a"], ["b"]]


def test_feed_split_mid_frame_returns_nothing_until_complete():
    p = FrameParser()
    raw = _frame([[1, 2, 3]])
    assert p.feed(raw[:5]) == []  # digits/length prefix only
    assert p.frames == []
    assert p.feed(raw[5:]) == [[1, 2, 3]]
    assert p.frames == [[1, 2, 3]]


def test_feed_one_char_at_a_time_handles_utf16_surrogates():
    # Emoji frame fed byte-by-byte: the length budget walk must not overshoot
    # (a 4-byte emoji counts as 2 UTF-16 units) and must stay resumable.
    payload = [["wrb.fr", None, "你好👋", None]]
    s = json.dumps(payload, ensure_ascii=False)
    units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)
    raw = f"{units + 2}\n\n{s}\n"
    p = FrameParser()
    got = []
    for i in range(len(raw)):
        got.extend(p.feed(raw[i]))
    assert got == [payload[0]]
    assert p._buf == ""  # no leftover, no over-advance


def _media_stream_frame(url, text="hi", status=1, cid="c_m", rid="r_m", rcid="rc_m"):
    """A StreamGenerate frame whose candidate carries one generated image."""
    inner: list = [None] * 43
    inner[1] = [cid, rid]
    candidate = [None] * 40
    candidate[0] = rcid
    candidate[1] = [text]
    candidate[8] = [status]
    candidate[12] = [None] * 90
    gen_img_data = [[None, None, None, [None, None, None, url]], "img_id"]
    candidate[12][7] = [[gen_img_data]]
    inner[4] = [candidate]
    return ["wrb.fr", None, json.dumps(inner), None, None, None, "generic"]


def test_generate_does_not_reprocess_frames_across_chunks(monkeypatch):
    frames = [
        _media_stream_frame("http://googleusercontent.com/gen/dup.png", text="here", status=1),
        _media_stream_frame("http://googleusercontent.com/gen/dup.png", text="here is", status=1),
        _media_stream_frame("http://googleusercontent.com/gen/dup.png", text="here it is", status=2),
    ]

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None,
                                          "__iter__": lambda self: iter(())})()
            self.headers = {}

        def get(self, url, **kw):
            return _ModelFakeInit()

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            return FakeResponse(frames)

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    client = GeminiWebClient({})
    seen = []
    res = client.generate("go", on_frame=seen.append)

    # Three frames on the wire -> three raw frames, three callbacks (not 3+2+1).
    assert len(res.raw_frames) == 3
    assert len(seen) == 3
    # The same image repeated by every frame collapses to one media entry.
    assert [m.url for m in res.media] == ["http://googleusercontent.com/gen/dup.png"]
    assert res.text == "here it is"
    assert res.completed is True


def test_consume_media_deduplicates_same_kind_and_url():
    candidate = [None] * 40
    candidate[0] = "rc_x"
    candidate[1] = ["answer"]
    candidate[12] = [None] * 90
    gen_img_data = [[None, None, None, [None, None, None, "http://x/1.png"]], "id"]
    candidate[12][7] = [[gen_img_data]]
    client = GeminiWebClient({})
    result = GenerationResult()
    frame = ["wrb.fr", None, json.dumps([None, None, None, None, [candidate]])]
    client._consume_frame(frame, result)
    client._consume_frame(frame, result)  # same candidate seen again
    assert len(result.media) == 1
    assert result.media[0].url == "http://x/1.png"


# ---------------------------------------------------------------------------
# Regression: transport errors + session self-healing (defect B)
# ---------------------------------------------------------------------------

def _session_with_cookies():
    """Fake session jar holding the two session cookies the client needs."""
    return type("J", (), {
        "set": lambda *a, **k: None,
        "__iter__": lambda self: iter([
            type("C", (), {"name": "__Secure-1PSID", "value": "psid",
                           "domain": ".google.com", "path": "/"}),
            type("C", (), {"name": "__Secure-1PSIDTS", "value": "ts",
                           "domain": ".google.com", "path": "/"}),
        ]),
    })()


def test_generate_wraps_transport_error_and_retries_with_backoff(monkeypatch):
    import requests as requests_module

    stream_posts = []
    delays = []

    class FakeSession:
        def __init__(self):
            self.cookies = _session_with_cookies()
            self.headers = {}

        def get(self, url, **kw):
            return _ModelFakeInit()

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            if "StreamGenerate" not in url:
                return _StatusResponse(200)  # RotateCookies
            stream_posts.append(url)
            if len(stream_posts) == 1:
                raise requests_module.ConnectionError("connection reset mid-stream")
            return FakeResponse([_stream_frame(text="recovered", status=2)])

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    monkeypatch.setattr(
        "gemini_mcp.gemini_client.time.sleep", lambda s: delays.append(s)
    )
    client = GeminiWebClient({})
    res = client.generate("ping")
    assert res.text == "recovered"
    assert delays == [0.8]  # first retry uses the first configured delay
    assert len(stream_posts) == 2


def test_generate_heals_stale_session_on_http_400_once(monkeypatch):
    stream_attempts = []
    rotates = []

    class FakeSession:
        def __init__(self):
            self.cookies = _session_with_cookies()
            self.headers = {}

        def get(self, url, **kw):
            return _ModelFakeInit()

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            if "RotateCookies" in url:
                rotates.append(url)
                return _StatusResponse(200)
            stream_attempts.append(url)
            if len(stream_attempts) == 1:
                return _StatusResponse(400)
            return FakeResponse([_stream_frame(text="after heal", status=2)])

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    monkeypatch.setattr("gemini_mcp.gemini_client.time.sleep", lambda s: None)
    client = GeminiWebClient({})
    res = client.generate("ping")
    assert res.text == "after heal"
    assert len(stream_attempts) == 2  # original + one post-heal retry
    assert rotates  # cookies were force-rotated before the retry


def test_generate_gives_up_after_bounded_retries(monkeypatch):
    import requests as requests_module

    stream_attempts = []
    delays = []

    class FakeSession:
        def __init__(self):
            self.cookies = _session_with_cookies()
            self.headers = {}

        def get(self, url, **kw):
            return _ModelFakeInit()

        def post(self, url, params=None, data=None, stream=None, timeout=None, headers=None):
            if "StreamGenerate" not in url:
                return _StatusResponse(200)  # RotateCookies
            stream_attempts.append(url)
            raise requests_module.Timeout("read timed out")

    monkeypatch.setattr("gemini_mcp.gemini_client.requests.Session", FakeSession)
    monkeypatch.setattr(
        "gemini_mcp.gemini_client.time.sleep", lambda s: delays.append(s)
    )
    client = GeminiWebClient({}, model="legacy")
    with pytest.raises(GeminiWebError) as exc:
        client.generate("ping")
    assert exc.value.transport is True
    # legacy chain = 1 plan: bounded by TRANSIENT_RETRY_ATTEMPTS (2), no hang.
    assert len(stream_attempts) == 3
    assert delays == [0.8, 2.0]
    assert "timed out" in str(exc.value).lower()
    assert isinstance(exc.value.__cause__, requests_module.RequestException)


class _StatusResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

