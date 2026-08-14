"""Tests for the Gemini web RPC client: frame parser, response consumption,
request construction and conversation RPCs (no network needed)."""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# Media: upload, generate-with-files, media parsing, download
# ---------------------------------------------------------------------------

def test_upload_file_two_phase_resumable(monkeypatch, tmp_path):
    captured = []
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")

    class FakeSession:
        def __init__(self):
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
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
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
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
    # generated video: [12][59][0][0][0] = video_info, url = video_info[0][7][1]
    video_info = [[None, None, None, None, None, None, None,
                   ["http://thumb.example/t.jpg", "http://video.example/v.mp4"]]]
    candidate[12][59] = [[[video_info]]]
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
            self.cookies = type("J", (), {"set": lambda *a, **k: None})()
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
