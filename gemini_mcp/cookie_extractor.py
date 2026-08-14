"""Cookie extractor ("decompiler") for Chrome / Edge / Chromium profiles.

Google's browsers store session cookies in a SQLite database with values
encrypted by the OS keychain:

* Windows  -> DPAPI (CryptUnprotectData) -> AES-256-GCM key from "Local State"
* macOS    -> Keychain item ("Chrome Safe Storage") -> PBKDF2 -> AES-128-CBC
* Linux    -> "Local State" blob protected by the hard-coded password "peanuts"
              -> AES-256-GCM

Values prefixed with "v10" are AES-GCM encrypted; older rows are plain text
or plain base64. The browser must be closed (or we copy the DB file first,
which we always do) so the SQLite file is not locked.

Usage (CLI)::

    python -m gemini_mcp.cookie_extractor --browser chrome --list
    python -m gemini_mcp.cookie_extractor --browser edge --profile "Profile 1" --reveal
"""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from Crypto.Cipher import AES  # pycryptodome

# ---------------------------------------------------------------------------
# Browser profile layout
# ---------------------------------------------------------------------------

BROWSERS: dict[str, dict[str, Any]] = {
    "chrome": {
        "local_state": {
            "linux": "~/.config/google-chrome/Local State",
            "darwin": "~/Library/Application Support/Google/Chrome/Local State",
            "win32": r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State",
        },
        "cookies": {
            "linux": "~/.config/google-chrome/{profile}/Cookies",
            "darwin": "~/Library/Application Support/Google/Chrome/{profile}/Cookies",
            "win32": r"%LOCALAPPDATA%\Google\Chrome\User Data\{profile}\Network\Cookies",
        },
        "keychain": "Chrome Safe Storage",
    },
    "edge": {
        "local_state": {
            "linux": "~/.config/microsoft-edge/Local State",
            "darwin": "~/Library/Application Support/Microsoft Edge/Local State",
            "win32": r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Local State",
        },
        "cookies": {
            "linux": "~/.config/microsoft-edge/{profile}/Cookies",
            "darwin": "~/Library/Application Support/Microsoft Edge/{profile}/Cookies",
            "win32": r"%LOCALAPPDATA%\Microsoft\Edge\User Data\{profile}\Network\Cookies",
        },
        "keychain": "Microsoft Edge Safe Storage",
    },
    "chromium": {
        "local_state": {
            "linux": "~/.config/chromium/Local State",
            "darwin": "~/Library/Application Support/Chromium/Local State",
            "win32": r"%LOCALAPPDATA%\Chromium\User Data\Local State",
        },
        "cookies": {
            "linux": "~/.config/chromium/{profile}/Cookies",
            "darwin": "~/Library/Application Support/Chromium/{profile}/Cookies",
            "win32": r"%LOCALAPPDATA%\Chromium\User Data\{profile}\Network\Cookies",
        },
        "keychain": "Chromium Safe Storage",
    },
}

# Older Chrome stored the DB directly under the profile dir.
_COOKIES_FALLBACK: dict[str, str] = {
    "linux": "~/.config/{browser}/{profile}/Cookies",
    "darwin": "~/Library/Application Support/{browser}/{profile}/Cookies",
    "win32": r"%LOCALAPPDATA%\{browser}\User Data\{profile}\Cookies",
}


@dataclass
class Cookie:
    name: str
    value: str
    host: str = ""
    path: str = "/"
    secure: bool = False
    http_only: bool = False
    expires: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "host": self.host,
            "path": self.path,
            "secure": self.secure,
            "http_only": self.http_only,
            "expires": self.expires,
        }


@dataclass
class ExtractionResult:
    browser: str
    profile: str
    cookies: list[Cookie] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self, include_values: bool = True) -> dict[str, Any]:
        return {
            "browser": self.browser,
            "profile": self.profile,
            "count": len(self.cookies),
            "cookies": [c.to_dict() for c in self.cookies] if include_values else [
                {**c.to_dict(), "value": "<hidden>"} for c in self.cookies
            ],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Platform decryption primitives
# ---------------------------------------------------------------------------

def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _local_state_path(browser: str) -> Optional[Path]:
    spec = BROWSERS[browser]["local_state"].get(platform.system().lower())
    if not spec:
        return None
    p = Path(_expand(spec))
    return p if p.is_file() else None


def _read_os_crypt_key_linux(local_state: Path) -> Optional[bytes]:
    """Linux: decrypt the AES-GCM key stored in Local State with 'peanuts'."""
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
        if encrypted_key.startswith(b"DPAPI"):
            encrypted_key = encrypted_key[5:]  # strip the "DPAPI" marker
        # Key derivation mirrors Chromium's os_crypt_linux.cc:
        # key = SHA256("peanuts"), IV = 16 spaces, AES-256-CBC.
        key = __import__("hashlib").sha256(b"peanuts").digest()
        iv = b" " * 16
        decrypted = AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted_key)
        return decrypted[:32]
    except Exception:
        return None


def _read_os_crypt_key_windows(local_state: Path) -> Optional[bytes]:
    """Windows: decrypt the AES-GCM key via DPAPI (CryptUnprotectData)."""
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
        if encrypted_key.startswith(b"DPAPI"):
            encrypted_key = encrypted_key[5:]
        return _dpapi_unprotect(encrypted_key)
    except Exception:
        return None


def _dpapi_unprotect(blob: bytes) -> Optional[bytes]:
    """Call CryptUnprotectData from crypt32.dll via ctypes."""
    if platform.system() != "Windows":
        return None
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]

    in_blob = _to_blob(blob)
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _read_os_crypt_key_macos(browser: str) -> Optional[bytes]:
    """macOS: read 'Chrome Safe Storage' from the Keychain via the security CLI."""
    service = BROWSERS[browser]["keychain"]
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True, check=True, text=True, timeout=15,
        )
        password = proc.stdout.strip()
        if not password:
            return None
        # macOS derives a 16-byte AES-128 key: PBKDF2-HMAC-SHA1(password, "saltysalt", 1003)
        import hashlib
        return hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, 16)
    except Exception:
        return None


def _get_decryption_key(browser: str, local_state: Optional[Path]) -> Optional[bytes]:
    system = platform.system().lower()
    if system == "linux":
        return _read_os_crypt_key_linux(local_state) if local_state else None
    if system == "win32":
        return _read_os_crypt_key_windows(local_state) if local_state else None
    if system == "darwin":
        return _read_os_crypt_key_macos(browser)
    return None


def _decrypt_value(encrypted: bytes, key: Optional[bytes]) -> str:
    """Decrypt one cookie value. Handles v10 (AES-GCM) and legacy formats."""
    if not encrypted:
        return ""
    if key and encrypted.startswith(b"v10"):
        # v10 layout: b"v10" + 12-byte nonce + ciphertext + 16-byte GCM tag
        try:
            nonce, ct, tag = encrypted[3:15], encrypted[15:-16], encrypted[-16:]
            plain = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
            return plain.decode("utf-8", errors="replace")
        except Exception:
            # macOS legacy cookies use AES-128-CBC with the 16-byte keychain key.
            if len(key) == 16:
                try:
                    cipher = AES.new(key, AES.MODE_CBC, iv=b" " * 16)
                    plain = cipher.decrypt(encrypted[3:])
                    pad = plain[-1]
                    if 0 < pad <= 16:
                        plain = plain[:-pad]
                    return plain.decode("utf-8", errors="replace")
                except Exception:
                    return ""
            return ""
    if encrypted.startswith(b"v10"):
        return ""  # encrypted but no usable key
    # Legacy: plain text or plain base64 (Linux pre-v80)
    try:
        return encrypted.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return base64.b64decode(encrypted).decode("utf-8", errors="replace")
        except Exception:
            return ""


def _is_clean(value: str) -> bool:
    """Reject values that are clearly garbage (undecodable/corrupted)."""
    if not value or "\ufffd" in value:
        return False
    return not any(ord(ch) < 32 and ch not in "\t" for ch in value)


# ---------------------------------------------------------------------------
# SQLite reading
# ---------------------------------------------------------------------------

def _find_cookie_db(browser: str, profile: str) -> Optional[Path]:
    system = platform.system().lower()
    candidates: list[Path] = []
    spec = BROWSERS[browser]["cookies"].get(system)
    if spec:
        candidates.append(Path(_expand(spec.format(profile=profile))))
    fallback = _COOKIES_FALLBACK.get(system)
    if fallback:
        candidates.append(Path(_expand(fallback.format(browser=browser, profile=profile))))
    for p in candidates:
        if p.is_file():
            return p
    return None


def _read_cookie_rows(db_path: Path) -> list[tuple[Any, ...]]:
    """Copy the DB to a temp file first (avoids SQLite locks from a running browser)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="gemini_mcp_cookies_"))
    tmp_db = tmp_dir / "Cookies"
    try:
        shutil.copy2(db_path, tmp_db)
        con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT name, value, host_key, path, expires_utc, is_secure, is_httponly, "
                "encrypted_value FROM cookies"
            )
            return cur.fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _local_state_diagnosis(local_state: Optional[Path]) -> str:
    """Explain why the encryption key could not be obtained (for diagnostics)."""
    if not local_state:
        return (
            "Local State file not found. Log in to gemini.google.com in the "
            "browser first and close it."
        )
    try:
        oc = json.loads(local_state.read_text(encoding="utf-8")).get("os_crypt", {})
    except Exception:
        return "Local State file exists but could not be read."
    if "encrypted_key" not in oc:
        return (
            "This browser uses the new 'portal'/app-bound cookie encryption "
            "(no legacy 'encrypted_key' in Local State), which cannot be "
            "decrypted offline. Export the cookies manually with a browser "
            "extension (e.g. Cookie-Editor) and pass them via --cookie-file."
        )
    return "The encrypted_key could not be decrypted (wrong platform/OS?)."


def extract_cookies(
    browser: str = "chrome",
    profile: str = "Default",
    domain_filter: Optional[str] = None,
) -> ExtractionResult:
    """Extract and decrypt cookies from a browser profile.

    ``domain_filter`` keeps only cookies whose host contains the given string
    (e.g. ``"google.com"``). ``None`` returns everything found.
    """
    result = ExtractionResult(browser=browser, profile=profile)
    if browser not in BROWSERS:
        result.errors.append(f"Unsupported browser '{browser}'. Use one of {list(BROWSERS)}")
        return result

    local_state = _local_state_path(browser)
    key = _get_decryption_key(browser, local_state)
    if not key and platform.system().lower() != "darwin":
        result.errors.append(_local_state_diagnosis(local_state))

    db_path = _find_cookie_db(browser, profile)
    if not db_path:
        result.errors.append(
            f"Cookie database not found for {browser}/{profile}. "
            "Log in to gemini.google.com in that browser first, then close it."
        )
        return result

    try:
        rows = _read_cookie_rows(db_path)
    except sqlite3.Error as e:
        result.errors.append(f"Failed to read cookie database {db_path}: {e}")
        return result

    seen: set[tuple[str, str]] = set()
    for row in rows:
        name, plain_value, host, path, expires_utc, is_secure, is_httponly, encrypted = (
            row[0], row[1], row[2], row[3], row[4], bool(row[5]), bool(row[6]), row[7]
        )
        if domain_filter and domain_filter not in (host or ""):
            continue
        value = plain_value or _decrypt_value(bytes(encrypted or b""), key)
        if not _is_clean(value) or (name, host) in seen:
            continue
        seen.add((name, host))
        # expires_utc: Chrome stores microseconds since 1601-01-01 (older) or
        # seconds since 1970 (newer). Normalize to unix seconds when sane.
        try:
            expires = float(expires_utc)
            if expires > 1e12:  # clearly microseconds-based
                expires = expires / 1_000_000 - 11_644_473_600.0
        except (TypeError, ValueError):
            expires = 0.0
        result.cookies.append(
            Cookie(
                name=name,
                value=value,
                host=host,
                path=path or "/",
                secure=bool(is_secure),
                http_only=bool(is_httponly),
                expires=max(expires, 0.0),
            )
        )

    if not result.cookies:
        result.errors.append(
            "No cookies found. Is the browser closed? Did you log in to google.com in it?"
        )
    return result


def cookies_for_gemini(
    browser: str = "chrome", profile: str = "Default"
) -> tuple[dict[str, str], list[str]]:
    """Shortcut: extract all google.com cookies as a plain name->value dict.

    Returns ``(cookies, errors)``. The dict is suitable for requests.Session.
    """
    res = extract_cookies(browser, profile, domain_filter="google.com")
    return {c.name: c.value for c in res.cookies}, res.errors


# ---------------------------------------------------------------------------
# CLI (debug / "decompile" viewer)
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="gemini-mcp-cookies",
        description="Extract and decrypt (decompile) cookies from a local Chrome/Edge profile.",
    )
    parser.add_argument("--browser", choices=list(BROWSERS), default="chrome")
    parser.add_argument("--profile", default="Default")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List cookie names found (values masked).",
    )
    parser.add_argument(
        "--reveal",
        action="store_true",
        help="Print decrypted cookie values (sensitive!).",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="Only show cookies whose host contains this string, e.g. google.com",
    )
    args = parser.parse_args(argv)

    res = extract_cookies(args.browser, args.profile, args.domain)
    print(f"browser={res.browser} profile={res.profile} cookies={len(res.cookies)}")
    for err in res.errors:
        print(f"WARN: {err}")
    if not args.list and not args.reveal:
        print("(use --list to enumerate names, --reveal to print values)")
    for c in res.cookies:
        value = c.value if args.reveal else "<hidden>"
        print(f"  {c.host:40s} {c.name:32s} {value}")
    return 0 if res.cookies else 1


if __name__ == "__main__":
    sys.exit(main())
