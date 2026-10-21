"""Shared bearer token for every Vibey HTTP service that listens on the LAN.

The services bind 0.0.0.0 so the phone app can reach them, which also means
anyone on the same Wi-Fi could. Rule: requests from this Mac (127.0.0.1 / ::1)
pass untouched, so the dashboard, the Mac app and the Telegram bot keep
working. Everything else needs `Authorization: Bearer <VIBEY_APP_TOKEN>`, or
`?token=` for things like an MJPEG <img> that can't set headers.

If VIBEY_APP_TOKEN is missing the LAN is simply closed (localhost only), never
open. Usage: `protect(HandlerClass)` once, after the class is defined.

"From this Mac" is not the same as "from Jack": any web page open in a browser
here also connects from 127.0.0.1. So two browser checks run first, for every
client. A request whose Origin is a different site is refused (a page on some
website can't drive the rover or switch privacy off). And the Host header must
be a name for this machine, which stops DNS rebinding (evil.com re-pointed at
127.0.0.1). Native clients (Python, the iOS app) send no Origin and pass. Extra
host names go in VIBEY_ALLOWED_HOSTS (comma separated).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
import urllib.parse
from pathlib import Path

_LOCAL = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


def _token() -> str:
    tok = os.environ.get("VIBEY_APP_TOKEN", "").strip()
    if tok:
        return tok
    # Services started without .env sourced still pick it up.
    try:
        for line in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("VIBEY_APP_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


TOKEN = _token()


def _hostname(value: str) -> str:
    """'Host: name:8772' or 'http://name:8770' -> 'name' (lowercase, no port)."""
    value = (value or "").strip().lower()
    if "://" in value:
        value = urllib.parse.urlparse(value).hostname or ""
    elif value.startswith("["):                       # [::1]:8772
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:                       # name:8772
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _machine_names() -> set[str]:
    names = {"localhost"}
    for n in (socket.gethostname(), socket.getfqdn()):
        n = (n or "").lower().rstrip(".")
        if n:
            names.add(n)
            names.add(n.split(".")[0])
            names.add(n.split(".")[0] + ".local")
    extra = os.environ.get("VIBEY_ALLOWED_HOSTS", "")
    names.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return names


_NAMES = _machine_names()


def host_ok(name: str) -> bool:
    """Is this a name the services can legitimately be reached by?"""
    if not name:
        return True                    # HTTP/1.0 clients send no Host
    try:
        ipaddress.ip_address(name)
        return True                    # a bare IP can't be DNS-rebound
    except ValueError:
        pass
    return (name in _NAMES or name.endswith(".local")
            or name.endswith(".ts.net"))


def origin_ok(handler) -> bool:
    """Browsers say which page made the request. Allow pages from this machine
    (any port: the dashboard on :8770 calls :8771/:8772) and nothing else."""
    origin = handler.headers.get("Origin")
    if origin is None:
        return True                    # not a cross-site browser request
    o = _hostname(origin)
    if not o:
        return False                   # "null": file:// pages, sandboxed iframes
    h = _hostname(handler.headers.get("Host", ""))
    return o == h or o in ("localhost", "127.0.0.1", "::1")


def allowed(handler) -> bool:
    if not host_ok(_hostname(handler.headers.get("Host", ""))):
        print(f"[auth] refused Host {handler.headers.get('Host')!r}; add it to "
              "VIBEY_ALLOWED_HOSTS if that's really this machine", flush=True)
        return False
    if not origin_ok(handler):
        print(f"[auth] refused cross-site request from "
              f"{handler.headers.get('Origin')!r}", flush=True)
        return False
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
