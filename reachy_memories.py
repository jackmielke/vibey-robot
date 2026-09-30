"""Vibey's memories: one short plain-text file per fact, in memories/.

This is the ONLY module that knows where memories are stored. Everything else
(the dashboard, the `remember` tool, the brain's prompt) goes through
list_all / read / write / add / delete. Moving the folder onto the robot's own
storage later means changing this file and nothing else.

Why it is still on the Mac (2026-09-30): the robot's REST API has no general
file store (only /api/media/sounds, which is for audio and not abused here),
and SSH to reachy@<robot> is key-locked. Once SSH works, swap the functions
below for sftp/ssh equivalents against e.g. ~/vibey/memories on the Pi.

File format: `memories/<YYYY-MM-DD>-<slug>.md`, the body is the memory text.
Hand-editable in any editor; a blank file is ignored.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

DIR = Path(os.environ.get("VIBEY_MEMORIES_DIR")
           or Path(__file__).resolve().parent / "memories")
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
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", p.name)
        out.append({"id": p.name, "text": text,
                    "date": m.group(1) if m else "",
                    "mtime": p.stat().st_mtime})
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
    return {"id": mid, "text": text}


def add(text: str, date: str | None = None) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("empty memory")
    DIR.mkdir(parents=True, exist_ok=True)
    date = date or time.strftime("%Y-%m-%d")
    base = f"{date}-{_slug(text)}"
    p, n = DIR / f"{base}.md", 2
    while p.exists():
        p, n = DIR / f"{base}-{n}.md", n + 1
    p.write_text(text + "\n", encoding="utf-8")
    return {"id": p.name, "text": text, "date": date}


def delete(mid: str) -> bool:
    p = _path(mid)
    if p.exists():
        p.unlink()
        return True
    return False


def prompt_lines(limit: int = 40) -> list[str]:
    """The most recent memories as `- text` lines for the brain's prompt."""
    return ["- " + " ".join(m["text"].split()) for m in list_all()[-limit:]]
