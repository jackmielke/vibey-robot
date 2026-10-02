#!/usr/bin/env python3
"""Front desk mode: Vibey scans Luma ticket QR codes at the door.

A Luma ticket QR is a URL like https://luma.com/check-in/evt-XXX?pk=g-XXXX.
The public Luma API can verify a guest by that key but cannot mark them
checked in, so Vibey keeps its own door log.

    camera :8771/frame.jpg ──▶ QR decode (OpenCV) ──▶ pk ──▶ guest list (CSV)
                                                            └─▶ Luma API (optional)
                                                     ──▶ :8772/sighting (the voice
                                                         brain says it) + /emote

Runs as its own small service on 127.0.0.1:8779 under .venv-gestures (the
chat venv has no OpenCV). reachy_chat.py proxies /frontdesk/* to it behind the
app token and starts it on demand, so nothing else has to know it exists.

Privacy: the log holds guest id, name, time and method only. No frames, faces
or raw ticket keys are ever written; a guest without a Luma id is keyed by a
hash. Front desk needs the camera, so it refuses to start while privacy mode is
on, and switches itself off if privacy comes back on.

CLI:
    reachy_frontdesk.py                 serve on :8779
    reachy_frontdesk.py decode IMG...   decode QR codes from image files
    reachy_frontdesk.py samples         write the sample guest list + QR PNGs
    reachy_frontdesk.py export          write the check-in log to CSV
    reachy_frontdesk.py clear           empty the check-in log
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIR = ROOT / "data" / "frontdesk"
DB_PATH = DIR / "checkins.sqlite"
STATE_PATH = DIR / "state.json"
SAMPLE_CSV = DIR / "sample_guests.csv"
PORT = int(os.environ.get("FRONTDESK_PORT", "8779"))
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:8772").rstrip("/")
CAMERA_URL = os.environ.get("CAMERA_URL", "http://localhost:8771").rstrip("/")
FPS = 5.0
DEBOUNCE_S = 10.0
LUMA_API = "https://public-api.luma.com/v1/events/guests/get"


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _privacy_on() -> bool:
    try:
        sys.path.insert(0, str(ROOT))
        import reachy_privacy
        return reachy_privacy.is_on()
    except Exception:  # noqa: BLE001 — unknown means private
        return True


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(".", " ").split())


def _first(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "friend"


# --------------------------------------------------------------------------- #
# Ticket keys
# --------------------------------------------------------------------------- #
def key_from_text(text: str) -> str | None:
    """The pk from a decoded QR: a Luma check-in URL, or a bare key."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" in text or text.startswith(("luma.com", "lu.ma")):
        u = urllib.parse.urlparse(text if "://" in text else "https://" + text)
        pk = (urllib.parse.parse_qs(u.query).get("pk") or [""])[0].strip()
        return pk or None
    if text.startswith("g-") and " " not in text and len(text) < 80:
        return text
    return None


def is_ticket_like(text: str) -> bool:
    t = (text or "").lower()
    return "check-in" in t or "pk=" in t or t.startswith("g-")


def _key_hash(pk: str) -> str:
    return "k-" + hashlib.sha256(pk.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Guest list (Luma CSV export, tolerant of column names)
# --------------------------------------------------------------------------- #
_COLS = {
    "id": ("api_id", "guest_api_id", "guest_id", "id"),
    "name": ("name", "full_name", "guest_name", "user_name"),
    "first": ("first_name", "firstname", "first"),
    "last": ("last_name", "lastname", "last"),
    "email": ("email", "email_address", "user_email"),
    "status": ("approval_status", "status", "registration_status"),
    "checked": ("checked_in_at", "checked_in", "check_in_time"),
    "key": ("pk", "ticket_key", "guest_key", "check_in_key", "key"),
    "url": ("qr_code_url", "qr_code", "qr_url", "check_in_url", "ticket_url"),
}


def _pick(row: dict, kind: str) -> str:
    lower = {(k or "").strip().lower().replace(" ", "_"): (v or "") for k, v in row.items()}
    for c in _COLS[kind]:
        if lower.get(c, "").strip():
            return lower[c].strip()
    return ""


def load_guests(path: str | Path) -> list[dict]:
    guests = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = _pick(row, "name") or " ".join(
                p for p in (_pick(row, "first"), _pick(row, "last")) if p)
            pk = _pick(row, "key") or key_from_text(_pick(row, "url")) or ""
            if not pk:   # any column holding a check-in URL will do
                for v in row.values():
                    if v and "pk=" in v:
                        pk = key_from_text(v) or ""
                        break
            email = _pick(row, "email").lower()
            if not (name or email or pk):
                continue
            guests.append({
                "id": _pick(row, "id") or (_key_hash(pk) if pk else "e-" + hashlib.sha256(
                    (email or name).encode()).hexdigest()[:12]),
                "name": name or email.split("@")[0],
                "email": email,
                "status": _pick(row, "status").lower() or "approved",
                "luma_checked_in": bool(_pick(row, "checked")),
                "pk": pk,
            })
    return guests


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #
_DB_LOCK = threading.Lock()


def _db() -> sqlite3.Connection:
    DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS checkins (guest_id TEXT PRIMARY KEY, "
              "name TEXT, at TEXT, method TEXT)")
    return c


def already_in(guest_id: str) -> bool:
    with _DB_LOCK, _db() as c:
        return c.execute("SELECT 1 FROM checkins WHERE guest_id=?", (guest_id,)).fetchone() is not None


def log_checkin(guest_id: str, name: str, method: str) -> None:
    with _DB_LOCK, _db() as c:
        c.execute("INSERT OR IGNORE INTO checkins VALUES (?,?,?,?)",
                  (guest_id, name, datetime.now().isoformat(timespec="seconds"), method))


def recent(n: int = 10) -> tuple[int, list[dict]]:
    with _DB_LOCK, _db() as c:
        total = c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0]
        rows = c.execute("SELECT name, at, method FROM checkins ORDER BY at DESC LIMIT ?",
                         (n,)).fetchall()
    return total, [{"name": r[0], "at": r[1], "method": r[2]} for r in rows]


def export_csv(path: Path | None = None) -> tuple[Path, str]:
    with _DB_LOCK, _db() as c:
        rows = c.execute("SELECT guest_id, name, at, method FROM checkins ORDER BY at").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["guest_id", "name", "checked_in_at", "method"])
    w.writerows(rows)
    path = path or DIR / f"checkins_{datetime.now():%Y%m%d_%H%M}.csv"
    path.write_text(buf.getvalue())
    return path, buf.getvalue()


def clear_log() -> None:
    with _DB_LOCK, _db() as c:
        c.execute("DELETE FROM checkins")


# --------------------------------------------------------------------------- #
# The desk
# --------------------------------------------------------------------------- #
class FrontDesk:
    def __init__(self):
        self.on = False
        self.csv_path = ""
        self.guests: list[dict] = []
        self.by_pk: dict[str, dict] = {}
        self.seen: dict[str, float] = {}      # decoded text -> last time in view
        self.last_error = ""
        self.frames = 0
        self.last_result: dict = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        try:
            st = json.loads(STATE_PATH.read_text())
            if st.get("csv") and Path(st["csv"]).exists():
                self.load(st["csv"])
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({"csv": self.csv_path}))

    def load(self, path: str) -> int:
        p = Path(os.path.expanduser(path)).resolve()
        guests = load_guests(p)
        with self._lock:
            self.guests = guests
            self.by_pk = {g["pk"]: g for g in guests if g["pk"]}
            self.csv_path = str(p)
        self._save()
        return len(guests)

    def api_ready(self) -> bool:
        return bool(_env("LUMA_API_KEY") and _env("LUMA_EVENT_ID"))

    # -- on / off --------------------------------------------------------- #
    def start(self) -> dict:
        if _privacy_on():
            return {"ok": False, "error": "Privacy mode is on. Front desk needs the "
                    "camera, so turn privacy off first."}
        if not self.guests and not self.api_ready():
            return {"ok": False, "error": "No guest list loaded. Load a Luma CSV "
                    "first (or set LUMA_API_KEY and LUMA_EVENT_ID)."}
        self.on = True
        if not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        print("[frontdesk] on", flush=True)
        return {"ok": True, **self.status()}

    def stop(self, why: str = "") -> dict:
        self.on = False
        print(f"[frontdesk] off{' (' + why + ')' if why else ''}", flush=True)
        return {"ok": True, **self.status()}

    def status(self) -> dict:
        n, last = recent(10)
        return {"on": self.on, "csv": self.csv_path, "guests": len(self.guests),
                "checked_in": n, "last": last, "privacy": _privacy_on(),
                "luma_api": self.api_ready(), "frames": self.frames,
                "error": self.last_error, "last_result": self.last_result}

    # -- scanning ----------------------------------------------------------- #
    def _grab(self) -> bytes | None:
        for path in ("/frame.jpg", "/frame"):
            try:
                with urllib.request.urlopen(CAMERA_URL + path, timeout=2) as r:
                    data = r.read()
                if data[:2] == b"\xff\xd8":
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                self.last_error = f"camera said {e.code}"
                return None
            except Exception as e:  # noqa: BLE001
                self.last_error = f"camera unreachable ({e.__class__.__name__})"
                return None
        self.last_error = "camera returned no JPEG"
        return None

    def _loop(self) -> None:
        import cv2
        import numpy as np
        det = cv2.QRCodeDetector()
        while self.on:
            t0 = time.time()
            if _privacy_on():
                self.stop("privacy mode came on")
                break
            jpg = self._grab()
            if jpg:
                img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
                del jpg
                if img is not None:
                    self.frames += 1
                    self.last_error = ""
                    try:
                        text = det.detectAndDecode(img)[0]
                    except cv2.error:
                        text = ""
                    del img
                    if text:
                        self._on_code(text)
            time.sleep(max(0.0, 1.0 / FPS - (time.time() - t0)))

    def _on_code(self, text: str) -> None:
        now = time.time()
        last = self.seen.get(text, 0.0)
        self.seen[text] = now          # held in view = still debounced
        if now - last < DEBOUNCE_S:
            return
        for k, t in list(self.seen.items()):
            if now - t > 120:
                del self.seen[k]
        if not is_ticket_like(text):
            print("[frontdesk] ignored a non-ticket QR", flush=True)
            return
        self.scan(text, method="qr")

    def _api_lookup(self, pk: str) -> dict | None:
        key, evt = _env("LUMA_API_KEY"), _env("LUMA_EVENT_ID")
        if not (key and evt):
            return None
        q = urllib.parse.urlencode({"event_id": evt, "id": pk})
        req = urllib.request.Request(f"{LUMA_API}?{q}", headers={
            "x-luma-api-key": key, "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                self.last_error = f"Luma API said {e.code}"
            return None
        except Exception as e:  # noqa: BLE001
            self.last_error = f"Luma API unreachable ({e.__class__.__name__})"
            return None
        g = data.get("guest") or data
        if not isinstance(g, dict) or not (g.get("api_id") or g.get("id")):
            return None
        return {"id": g.get("api_id") or g.get("id"),
                "name": g.get("user_name") or g.get("name") or "",
                "email": (g.get("user_email") or g.get("email") or "").lower(),
                "status": (g.get("approval_status") or "approved").lower(),
                "luma_checked_in": bool(g.get("checked_in_at")), "pk": pk}

    def scan(self, text: str, method: str = "qr", quiet: bool = False) -> dict:
        """One decoded QR. Returns the result; speaks it unless quiet."""
        pk = key_from_text(text)
        guest = self.by_pk.get(pk) if pk else None
        if guest is None and pk:
            guest = self._api_lookup(pk)
        return self._admit(guest, method, quiet)

    def _admit(self, guest: dict | None, method: str, quiet: bool) -> dict:
        if guest is None:
            res = {"result": "not_found"}
        elif guest["status"] not in ("approved", "going", "confirmed", ""):
            res = {"result": "not_approved", "name": guest["name"], "status": guest["status"]}
        elif already_in(guest["id"]) or guest.get("luma_checked_in"):
            res = {"result": "already_in", "name": guest["name"]}
        else:
            log_checkin(guest["id"], guest["name"], method)
            res = {"result": "checked_in", "name": guest["name"]}
        res["method"] = method
        res["at"] = datetime.now().isoformat(timespec="seconds")
        self.last_result = res
        print(f"[frontdesk] {method}: {res['result']}", flush=True)
        try:
            import reachy_events
            reachy_events.emit("senses", f"front desk {method}: {res['result'].replace('_', ' ')}"
                               + (f" · {res['name']}" if res.get("name") else ""),
                               detail=res, source="frontdesk", icon="🎟")
        except Exception:  # noqa: BLE001
            pass
        if not quiet:
            threading.Thread(target=announce, args=(res,), daemon=True).start()
        return res

    def check_in_by_name(self, query: str, quiet: bool = True) -> dict:
        """The voice fallback: a guest says their name or email."""
        q = _norm(query)
        if not q:
            return {"result": "need_name"}
        if "@" in query:
            email = "".join(query.lower().split())
            hit = [g for g in self.guests if g["email"] == email]
            return self._admit(hit[0] if hit else None, "voice", quiet)
        scored = []
        for g in self.guests:
            full = _norm(g["name"])
            s = difflib.SequenceMatcher(None, q, full).ratio()
            if len(q.split()) == 1:
                s = max(s, 0.92 * difflib.SequenceMatcher(None, q, full.split(" ")[0]).ratio())
            scored.append((s, g))
        scored.sort(key=lambda t: -t[0])
        if not scored or scored[0][0] < 0.75:
            return self._admit(None, "voice", quiet)
        close = [g for s, g in scored if s >= scored[0][0] - 0.05 and s >= 0.75]
        if len(close) > 1:
            return {"result": "ambiguous", "candidates": [g["name"] for g in close[:4]]}
        return self._admit(scored[0][1], "voice", quiet)


def spoken_line(res: dict) -> str:
    r, first = res.get("result"), _first(res.get("name", ""))
    if r == "checked_in":
        return f"hold there a second... okay {first}, you're good! welcome in"
    if r == "already_in":
        return f"{first}, you're already in! go on through"
    if r == "not_approved":
        return "hmm, your ticket isn't approved yet. grab a human at the door"
    if r == "ambiguous":
        return "I've got a couple of people by that name. what's your last name?"
    return "hmm, I can't find that ticket. grab a human at the door"


_EMOTE = {"checked_in": "happy", "already_in": "wink",
          "not_found": "confused", "not_approved": "confused"}


def _post(path: str, body: dict, timeout: float = 5) -> dict:
    req = urllib.request.Request(CHAT_URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def announce(res: dict) -> None:
    """Hand the line to whichever brain holds the conversation, so it comes out
    in Vibey's own voice. With no live session, say it verbatim instead: at a
    door, silence is worse than a different voice."""
    line = spoken_line(res)
    try:
        if res.get("result") in _EMOTE:
            _post("/emote", {"name": _EMOTE[res["result"]], "sound": False})
    except Exception as e:  # noqa: BLE001
        print(f"[frontdesk] emote failed: {e}", flush=True)
    nudge = (f"FRONT DESK SCAN RESULT: {res.get('result')}. Say this right now, "
             f"in one short upbeat line and nothing more: \"{line}\"")
    try:
        out = _post("/sighting", {"text": nudge})
        if out.get("delivered") == "realtime":
            return
        _post("/say", {"text": line})
    except Exception as e:  # noqa: BLE001
        print(f"[frontdesk] speech failed: {e}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP (127.0.0.1 only; the phone reaches it through :8772/frontdesk/*)
# --------------------------------------------------------------------------- #
DESK: FrontDesk | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p in ("", "/status"):
            self._json(DESK.status())
        elif p == "/export":
            path, text = export_csv()
            self._json({"ok": True, "path": str(path), "csv": text})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        try:
            b = self._body()
            if p == "/on":
                out = DESK.start()
                self._json(out, 200 if out.get("ok") else 409)
            elif p == "/off":
                self._json(DESK.stop())
            elif p == "/toggle":
                out = DESK.start() if b.get("on", not DESK.on) else DESK.stop()
                self._json(out, 200 if out.get("ok") else 409)
            elif p == "/load":
                path = str(b.get("path") or "").strip()
                if not path:
                    raise ValueError("path required")
                self._json({"ok": True, "guests": DESK.load(path), "csv": DESK.csv_path})
            elif p == "/scan":
                # A decoded QR, as if the camera had seen it. For tests.
                self._json(DESK.scan(str(b.get("text", "")), method=str(b.get("method") or "qr"),
                                     quiet=bool(b.get("quiet"))))
            elif p == "/checkin":
                q = str(b.get("name_or_email") or b.get("name") or b.get("email") or "")
                res = DESK.check_in_by_name(q, quiet=bool(b.get("quiet", True)))
                self._json({**res, "say": spoken_line(res)})
            elif p == "/export":
                path, text = export_csv()
                self._json({"ok": True, "path": str(path), "csv": text})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


def serve() -> None:
    global DESK
    DESK = FrontDesk()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"[frontdesk] on http://127.0.0.1:{PORT} · {len(DESK.guests)} guests "
          f"from {DESK.csv_path or 'nothing yet'}", flush=True)
    srv.serve_forever()


# --------------------------------------------------------------------------- #
# Samples and decoding from files
# --------------------------------------------------------------------------- #
SAMPLE_EVENT = "evt-VibeyDoorDemo"
_SAMPLE = [
    ("gst-s1Maya", "Maya Okafor", "maya.okafor@example.com", "approved", "g-Mq7Tz2pLkV"),
    ("gst-s2Theo", "Theo Lindqvist", "theo.l@example.com", "approved", "g-T4hWn8cRxa"),
    ("gst-s3Priya", "Priya Raman", "priya.raman@example.com", "approved", "g-P9sKd3vYbe"),
    ("gst-s4Diego", "Diego Alvarez", "diego@example.com", "approved", "g-D2fQm7uJtc"),
    ("gst-s5Hana", "Hana Kobayashi", "hana.k@example.com", "approved", "g-H6gLp1wNzo"),
    ("gst-s6Sam", "Sam Whitfield", "sam.whitfield@example.com", "pending_approval", "g-S3jVr5eXqi"),
    ("gst-s7Lena", "Lena Novak", "lena.novak@example.com", "approved", "g-L8bCx4tHmu"),
    ("gst-s8Omar", "Omar Haddad", "omar.h@example.com", "declined", "g-O1nZk9sGwy"),
    ("gst-s9Maya", "Maya Chen", "maya.chen@example.com", "approved", "g-C5rTe2dPva"),
    ("gst-s10Jo", "Jo Brennan", "jo.brennan@example.com", "approved", "g-J7kYw3mQfs"),
]


def _qr_png(text: str, path: Path, label: str) -> None:
    import cv2
    img = cv2.QRCodeEncoder.create().encode(text)
    side = 12 * img.shape[0]
    img = cv2.resize(img, (side, side), interpolation=cv2.INTER_NEAREST)
    img = cv2.copyMakeBorder(img, 60, 110, 60, 60, cv2.BORDER_CONSTANT, value=255)
    cv2.putText(img, label, (60, img.shape[0] - 45), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2,
                cv2.LINE_AA)
    cv2.imwrite(str(path), img)


def write_samples() -> list[Path]:
    """A Luma-shaped guest list with fictional people, plus QR PNGs to hold up."""
    DIR.mkdir(parents=True, exist_ok=True)
    cols = ["api_id", "name", "first_name", "last_name", "email", "phone_number",
            "created_at", "approval_status", "checked_in_at", "custom_source",
            "qr_code_url", "amount", "currency", "ticket_type_id", "ticket_name"]
    with open(SAMPLE_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for gid, name, email, status, pk in _SAMPLE:
            first, last = name.split(" ", 1)
            w.writerow([gid, name, first, last, email, "", "2026-09-28T18:04:11.000Z",
                        status, "", "", f"https://luma.com/check-in/{SAMPLE_EVENT}?pk={pk}",
                        "0", "USD", "evtticktyp-sampleGA", "General Admission"])
    qr_dir = DIR / "sample_qr"
    qr_dir.mkdir(exist_ok=True)
    out = []
    picks = [_SAMPLE[0], _SAMPLE[8], _SAMPLE[5]]
    for gid, name, _e, status, pk in picks:
        p = qr_dir / f"{name.split()[0].lower()}_{name.split()[1].lower()}.png"
        _qr_png(f"https://luma.com/check-in/{SAMPLE_EVENT}?pk={pk}", p,
                f"{name} ({status})")
        out.append(p)
    p = qr_dir / "unknown_ticket.png"
    _qr_png(f"https://luma.com/check-in/{SAMPLE_EVENT}?pk=g-NotOnTheList", p, "not on the list")
    out.append(p)
    return out


def decode_file(path: str) -> str:
    import cv2
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"can't read {path}")
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "decode":
        for f in sys.argv[2:]:
            t = decode_file(f)
            print(f"{f}: {t!r} -> pk {'found' if key_from_text(t) else 'none'}")
    elif cmd == "samples":
        for p in write_samples():
            print(p)
        print(SAMPLE_CSV)
    elif cmd == "export":
        print(export_csv()[0])
    elif cmd == "clear":
        clear_log()
        print("check-in log cleared")
    else:
        print(__doc__)
