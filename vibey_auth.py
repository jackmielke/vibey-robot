"""Shared bearer token for every Vibey HTTP service that listens on the LAN.

The services bind 0.0.0.0 so the phone app can reach them, which also means
anyone on the same Wi-Fi could. Rule: requests from this Mac (127.0.0.1 / ::1)
pass untouched, so the dashboard, the Mac app and the Telegram bot keep
working. Everything else needs `Authorization: Bearer <VIBEY_APP_TOKEN>`, or
`?token=` for things like an MJPEG <img> that can't set headers.

If VIBEY_APP_TOKEN is missing the LAN is simply closed (localhost only), never
open. Usage: `protect(HandlerClass)` once, after the class is defined.
"""
from __future__ import annotations

import hmac
import json
import os
import urllib.parse
from pathlib import Path

_LOCAL = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


def _token() -> str:
    tok = os.environ.get("VIBEY_APP_TOKEN", "").strip()
    if tok:
        return tok
    # Services started without .env sourced still pick it up.
    try:
        for line in (Path(__file__).resolve().parent / ".env").read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("VIBEY_APP_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


TOKEN = _token()


def allowed(handler) -> bool:
    host = (handler.client_address or ("",))[0]
    if host in _LOCAL or host.startswith("127."):
        return True
    if not TOKEN:
        return False
    got = handler.headers.get("Authorization", "")
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    else:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        got = (q.get("token") or [""])[0]
    return bool(got) and hmac.compare_digest(got.encode(), TOKEN.encode())


def _deny(handler) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("WWW-Authenticate", 'Bearer realm="vibey"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def protect(cls):
    """Wrap every do_* method on a BaseHTTPRequestHandler subclass."""
    for name in ("do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE",
                 "do_OPTIONS", "do_HEAD"):
        fn = cls.__dict__.get(name) or getattr(cls, name, None)
        if fn is None:
            continue

        def guarded(self, _fn=fn):
            if not allowed(self):
                _deny(self)
                return
            return _fn(self)

        guarded.__name__ = name
        setattr(cls, name, guarded)
    return cls
