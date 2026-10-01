"""Vibey's memories: one short plain-text file per fact, in memories/.

This is the ONLY module that knows where memories are stored. Everything else
(the dashboard, the `remember` tool, the brain's prompt) goes through
list_all / read / write / add / delete. Moving the folder onto the robot's own
storage later means changing this file and nothing else.

Where they live (2026-09-30): ON THE ROBOT, in ~/vibey/memories on its CM4
(ssh pollen@<robot>, key login). memories/ on the Mac is a cache: every read
first syncs with the robot (Mac edits made while it was offline go up, then
the robot's copy comes down), every edit is pushed straight to it, and if the
robot is unreachable everything keeps working from the cache.

File format: `memories/<YYYY-MM-DD>-<HHMM>-<slug>.md` (older files have no HHMM), the body is the memory text.
Hand-editable in any editor; a blank file is ignored.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

DIR = Path(os.environ.get("VIBEY_MEMORIES_DIR")
           or Path(__file__).resolve().parent / "memories")
_HOST = (urlparse(os.environ.get("REACHY_URL", "")).hostname
         or "reachy-mini.local")
ROBOT = os.environ.get("VIBEY_ROBOT_SSH") or f"pollen@{_HOST}"
ROBOT_DIR = "vibey/memories"   # relative to the robot user's home
_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3"]
_SYNC_EVERY = 10.0
_last_sync = [0.0]
def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _rsync(src: str, dst: str, *extra: str) -> bool:
    return _run(["rsync", "-a", *extra, "-e", " ".join(_SSH), src, dst])


def sync(force: bool = False) -> bool:
    """Robot is the source of truth. Push up anything newer on the Mac, then
    mirror the robot's folder down. Returns False if the robot didn't answer."""
    if not force and time.time() - _last_sync[0] < _SYNC_EVERY:
        return True
    _last_sync[0] = time.time()
    DIR.mkdir(parents=True, exist_ok=True)
    if not _run(_SSH + [ROBOT, f"mkdir -p {ROBOT_DIR}"]):
        return False
    _rsync(f"{DIR}/", f"{ROBOT}:{ROBOT_DIR}/", "--update", "--include=*.md", "--exclude=*")
    return _rsync(f"{ROBOT}:{ROBOT_DIR}/", f"{DIR}/", "--delete",
                  "--include=*.md", "--exclude=*")


def _push(p: Path) -> None:
    _rsync(str(p), f"{ROBOT}:{ROBOT_DIR}/{p.name}")


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")


def _path(mid: str) -> Path:
    if not _ID.match(mid or ""):
        raise ValueError("bad memory id")
    return DIR / mid


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:6])[:48] or "memory"


def list_all() -> list[dict]:
    """Oldest first. [{id, text, date, mtime}]"""
    sync()
    if not DIR.is_dir():
        return []
    out = []
    for p in DIR.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-(\d{2})(\d{2})(?=-))?", p.name)
        mtime = p.stat().st_mtime
        day = m.group(1) if m else ""
        if m and m.group(2):
            hhmm = f"{m.group(2)}:{m.group(3)}"
        elif day and time.strftime("%Y-%m-%d", time.localtime(mtime)) == day:
            # Older files have no time in the name; the file's own timestamp
            # is trustworthy only when it's from the same day as the name.
            hhmm = time.strftime("%H:%M", time.localtime(mtime))
        else:
            hhmm = ""
        out.append({"id": p.name, "text": text, "day": day, "time": hhmm,
                    "date": f"{day} {hhmm}".strip(), "mtime": mtime})
    out.sort(key=lambda m: (m["id"][:10] if m["date"] else "9999", m["mtime"], m["id"]))
    return out


def read(mid: str) -> str:
    return _path(mid).read_text(encoding="utf-8").strip()


def write(mid: str, text: str) -> dict:
    """Overwrite an existing memory's text."""
    p = _path(mid)
    if not p.exists():
        raise FileNotFoundError(mid)
    text = text.strip()
    p.write_text(text + "\n", encoding="utf-8")
    _push(p)
    return {"id": mid, "text": text}


def add(text: str, date: str | None = None) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("empty memory")
    DIR.mkdir(parents=True, exist_ok=True)
    date = date or time.strftime("%Y-%m-%d")
    base = f"{date}-{time.strftime('%H%M')}-{_slug(text)}"
    p, n = DIR / f"{base}.md", 2
    while p.exists():
        p, n = DIR / f"{base}-{n}.md", n + 1
    p.write_text(text + "\n", encoding="utf-8")
    _push(p)
    return {"id": p.name, "text": text, "date": date}


def delete(mid: str) -> bool:
    p = _path(mid)
    _run(_SSH + [ROBOT, f"rm -f {ROBOT_DIR}/{mid}"])   # mid is validated by _path
    if p.exists():
        p.unlink()
        return True
    return False


def prompt_lines(limit: int = 40) -> list[str]:
    """The most recent memories as `- text` lines for the brain's prompt."""
    return ["- " + " ".join(m["text"].split()) for m in list_all()[-limit:]]
