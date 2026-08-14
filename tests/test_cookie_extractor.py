"""Tests for the cookie "decompiler" using synthetic encrypted fixtures.

These tests build encrypted cookie blobs the same way Chromium does, so no
real browser data is needed.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from Crypto.Cipher import AES

from gemini_mcp import cookie_extractor as ce


def _gcm_cookie(key: bytes, value: str) -> bytes:
    """Encrypt a value like Chrome v80+ (AES-256-GCM, 'v10' prefix)."""
    nonce = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(value.encode())
    return b"v10" + nonce + ct + tag


def _linux_local_state(key: bytes) -> dict:
    """Build a Local State dict like Chrome on Linux (peanuts-protected key)."""
    password_key = hashlib.sha256(b"peanuts").digest()
    iv = b" " * 16
    cipher = AES.new(password_key, AES.MODE_CBC, iv)
    padded = key + b"\x00" * (16 - len(key) % 16)
    encrypted = cipher.encrypt(padded)
    return {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + encrypted).decode()}}


# ---------------------------------------------------------------------------
# _decrypt_value
# ---------------------------------------------------------------------------

def test_decrypt_v10_gcm_roundtrip():
    key = b"k" * 32
    blob = _gcm_cookie(key, "hello-gemini")
    assert ce._decrypt_value(blob, key) == "hello-gemini"


def test_decrypt_v10_wrong_key_returns_empty():
    blob = _gcm_cookie(b"a" * 32, "secret")
    assert ce._decrypt_value(blob, b"b" * 32) == ""


def test_decrypt_v10_without_key_returns_empty():
    blob = _gcm_cookie(b"a" * 32, "secret")
    assert ce._decrypt_value(blob, None) == ""


def test_decrypt_plain_and_base64_legacy():
    assert ce._decrypt_value(b"plain-value", None) == "plain-value"
    # base64 fallback only kicks in when raw bytes are not valid UTF-8
    blob = base64.b64encode(b"legacy-value") + b"\xff"
    assert ce._decrypt_value(blob, None) == "legacy-value"


def test_decrypt_empty():
    assert ce._decrypt_value(b"", None) == ""


# ---------------------------------------------------------------------------
# Linux Local State key extraction
# ---------------------------------------------------------------------------

def test_linux_local_state_key_roundtrip(tmp_path):
    key = bytes(range(32))
    state = _linux_local_state(key)
    state_file = tmp_path / "Local State"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert ce._read_os_crypt_key_linux(state_file) == key


def test_linux_local_state_missing(tmp_path):
    assert ce._read_os_crypt_key_linux(tmp_path / "nope") is None


# ---------------------------------------------------------------------------
# End-to-end: Linux-style profile → extracted cookie dict
# ---------------------------------------------------------------------------

def test_extract_cookies_linux_style(tmp_path, monkeypatch):
    key = bytes(range(32))
    state = _linux_local_state(key)

    # Build a fake profile dir with Local State + Cookies DB
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Local State").write_text(json.dumps(state), encoding="utf-8")

    import sqlite3

    db = profile / "Cookies"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE cookies (
            name TEXT, value TEXT, host_key TEXT, path TEXT,
            expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER,
            encrypted_value BLOB)"""
    )
    con.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)",
        ("__Secure-1PSID", "", ".google.com", "/", 0, 1, 1, _gcm_cookie(key, "PSID-VALUE")),
    )
    con.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)",
        ("__Secure-1PSIDTS", "", ".google.com", "/", 0, 1, 1, _gcm_cookie(key, "PSIDTS-VALUE")),
    )
    con.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)",
        ("SID", "plain-sid", ".google.com", "/", 0, 0, 0, b""),
    )
    con.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)",
        ("NID", "", "example.org", "/", 0, 0, 0, b"\x00" * 30),  # filtered out by domain
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ce.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ce,
        "_local_state_path",
        lambda browser: profile / "Local State",
    )
    monkeypatch.setattr(ce, "_find_cookie_db", lambda browser, prof: db)

    cookies, errors = ce.cookies_for_gemini("chrome")
    assert not errors, errors
    assert cookies["__Secure-1PSID"] == "PSID-VALUE"
    assert cookies["__Secure-1PSIDTS"] == "PSIDTS-VALUE"
    assert cookies["SID"] == "plain-sid"
    assert "NID" not in cookies


def test_extract_cookies_profile_missing(monkeypatch):
    monkeypatch.setattr(ce.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ce, "_find_cookie_db", lambda browser, prof: None)
    res = ce.extract_cookies("chrome", "Default")
    assert not res.cookies
    assert any("not found" in e for e in res.errors)


def test_unsupported_browser():
    res = ce.extract_cookies("netscape")
    assert "Unsupported browser" in res.errors[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_list_masks_values(capsys, monkeypatch):
    class FakeResult:
        browser, profile = "chrome", "Default"
        cookies = [ce.Cookie(name="SID", value="topsecret", host=".google.com")]
        errors = []

    monkeypatch.setattr(ce, "extract_cookies", lambda *a, **k: FakeResult())
    assert ce.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "topsecret" not in out
    assert "SID" in out


def test_cli_reveal_shows_values(capsys, monkeypatch):
    class FakeResult:
        browser, profile = "chrome", "Default"
        cookies = [ce.Cookie(name="SID", value="topsecret", host=".google.com")]
        errors = []

    monkeypatch.setattr(ce, "extract_cookies", lambda *a, **k: FakeResult())
    assert ce.main(["--reveal"]) == 0
    assert "topsecret" in capsys.readouterr().out
