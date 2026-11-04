"""Refresh ~/.config/gemini-web-mcp/cookies.json from Cherry Studio's webview profile.

Cherry Studio (Electron) keeps gemini.google.com's session in a plaintext-value
Chromium cookie database, so it can be copied and read directly -- but only while
the app is closed, because Chromium takes an exclusive lock on the file.

Usage:
    python tools/refresh_cookies.py            # print what it would do
    python tools/refresh_cookies.py --write    # write cookies.json (keeps a .bak)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

PROFILE = Path(r"C:\Users\sir_c\AppData\Roaming\CherryStudio\Partitions\webview\Network")
TARGET = Path.home() / ".config" / "gemini-web-mcp" / "cookies.json"


def copy_db() -> Path:
    src = PROFILE / "Cookies"
    if not src.is_file():
        raise SystemExit(f"cookie database not found: {src}")
    cache = Path(__file__).resolve().parent.parent / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / "cs_cookies_refresh.db"
    shutil.copy2(src, tmp)  # raises PermissionError while Cherry Studio is open
    return tmp


def read_cookies(db: Path) -> dict[str, str]:
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "select name, value from cookies where host_key = '.google.com' and value <> ''"
        ).fetchall()
    finally:
        con.close()
    return {name: value for name, value in rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write cookies.json")
    parser.add_argument("--out", default=None, help="write to this path instead of the default")
    args = parser.parse_args()

    jar = read_cookies(copy_db())
    missing = [k for k in ("SID", "__Secure-1PSID", "__Secure-1PSIDTS") if k not in jar]
    print(f"cookies: {len(jar)}")
    print(f"critical missing: {missing or 'none'}")
    if not args.write:
        print("dry run; pass --write to persist")
        return

    target = Path(args.out) if args.out else TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        backup = target.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)
        print(f"backup: {backup.name}")
    target.write_text(json.dumps(jar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()