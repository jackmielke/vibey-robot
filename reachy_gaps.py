"""reachy_gaps.py — one audit of everything that is missing, as rows.

The dashboard already shows a dozen live truths, each in its own panel: is the
camera up, is the robot linked, who do I know, what did I log. What it never
showed is the negative space — the things that are *absent*. Absence has no
panel of its own, so it hid: a face nobody named, a service that quietly died,
memories sitting in the Mac cache because the robot was asleep when they were
written. Every one of those is invisible until somebody notices the symptom.

`scan()` runs every cheap check I know how to run and returns one flat list of
rows, gaps and clean checks alike. The dashboard shows the gaps and folds the
clean ones into a single line, so the table is short when the house is in order
and long exactly when it should be.

Three states, and the third one matters: `gap` (something is missing), `clean`
(checked, nothing missing), `unknown` (the check itself could not run — a bad
import, a locked database). An `unknown` is never silently counted as clean;
saying "I could not tell" is the honest answer and it stays on screen.

Every row carries `basis`: the literal thing that was measured. A gap that
cannot say how it was derived is a guess wearing a badge, and this file is read
by whoever is trying to fix the robot at the time.

Read-only and stdlib-only: TCP connects to localhost, one connect to the robot,
local file stats, local SQLite. Nothing is started, written or fixed here —
each row instead carries the *one* action that would resolve it, either a shell
command to copy or a panel on this page to jump to.
"""
from __future__ import annotations

import datetime as _dt
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
VENV = "source reachy_env/bin/activate && "

# port -> (area label, what a person calls it, the script that serves it,
#          severity when it is down)
SERVICES: dict[int, tuple[str, str, str]] = {
    8771: ("Camera feed", "reachy_camera.py", "blocker"),
    8772: ("Voice chat", "reachy_chat.py", "blocker"),
    8773: ("Face memory", "reachy_memory.py", "degraded"),
    8774: ("VibeVerse bridge", "reachy_vibeverse.py", "polish"),
    8775: ("Robot mic", "reachy_robot_mic.py", "degraded"),
    8778: ("DJ", "reachy_dj.py", "polish"),
}

WEAK_SAMPLES = 3        # below this, recognition is a coin flip in new light
STALE_NOTE_DAYS = 3

_CACHE: tuple[float, dict] | None = None
_TTL = 15.0             # the panel polls; the checks are cheap but not free


# ----------------------------------------------------------------- utilities
def _row(rid: str, area: str, title: str, state: str, severity: str,
         detail: str, why: str, basis: str, fix: dict | None = None) -> dict:
    return {"id": rid, "area": area, "title": title, "state": state,
            "severity": severity if state == "gap" else "info",
            "detail": detail, "why": why, "basis": basis,
            "fix": fix or {"kind": "none", "label": ""}}


def _listening(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _today() -> str:
    return _dt.date.today().isoformat()


def _newest(dirname: str, suffix: str) -> tuple[str | None, int]:
    d = HERE / dirname
    if not d.is_dir():
        return None, 0
    files = sorted(f.name for f in d.iterdir() if f.name.endswith(suffix))
    return (files[-1] if files else None), len(files)


def _days_old(name: str) -> int | None:
    """Files here are named <YYYY-MM-DD>-something; age in days, or None."""
    try:
        d = _dt.date.fromisoformat(name[:10])
    except ValueError:
        return None
    return (_dt.date.today() - d).days


# -------------------------------------------------------------------- checks
def _check_link() -> list[dict]:
    url = os.environ.get("REACHY_URL", "http://192.168.1.120:8000")
    u = urlparse(url)
    host, port = u.hostname or "reachy-mini.local", u.port or 80
    up = False
    try:
        up = _listening(port, host, timeout=1.5)
    except OSError:
        up = False       # unresolvable hostname is also "not reachable"
    if up:
        return [_row("link", "Robot link", "Robot is reachable", "clean", "info",
                     f"{host}:{port} answered", "", f"TCP connect to {host}:{port}")]
    return [_row("link", "Robot link", "Robot is not answering", "gap", "blocker",
                 f"nothing accepted a connection on {host}:{port}",
                 "no link means no motion, no speaker, and memories cannot sync",
                 f"TCP connect to {host}:{port}",
                 {"kind": "jump", "label": "Find the robot", "target": "connpanel"})]


def _check_services() -> list[dict]:
    out = []
    for port, (name, script, sev) in SERVICES.items():
        if _listening(port):
            out.append(_row(f"svc{port}", "Services", f"{name} is up", "clean",
                            "info", f"port {port} listening", "",
                            f"TCP connect to 127.0.0.1:{port}"))
        else:
            out.append(_row(
                f"svc{port}", "Services", f"{name} is down", "gap", sev,
                f"nothing listening on port {port}",
                {"blocker": "a headline feature of the demo is simply absent",
                 "degraded": "everything still runs, this part just goes quiet",
                 "polish": "only noticed if somebody asks for it"}[sev],
                f"TCP connect to 127.0.0.1:{port} refused",
                {"kind": "cmd", "label": "Copy start command",
                 "cmd": f"cd {HERE} && {VENV}python3 {script}"}))
    return out


def _check_memories(robot_up: bool) -> list[dict]:
    out = []
    try:
        import reachy_memories
        files = [p.name for p in Path(reachy_memories.DIR).glob("*.md")]
    except Exception as e:  # noqa: BLE001
        return [_row("mem", "Memory", "Cannot read the memory folder", "unknown",
                     "info", str(e)[:140], "",
                     "import reachy_memories + glob memories/*.md")]
    if not files:
        out.append(_row("mem-empty", "Memory", "No memories written yet", "gap",
                        "degraded", "memories/ is empty",
                        "the brain's prompt has nothing personal in it",
                        "glob memories/*.md returned 0 files",
                        {"kind": "say", "label": "Ask Vibey to remember something",
                         "text": "remember that "}))
    elif not robot_up:
        out.append(_row("mem-cache", "Memory",
                        f"{len(files)} memories live only in the Mac cache", "gap",
                        "degraded",
                        "the robot is the source of truth and it did not answer",
                        "edits are safe, but they are one laptop away from being lost",
                        "memories/*.md counted locally; robot link is down",
                        {"kind": "jump", "label": "Fix the link", "target": "connpanel"}))
    else:
        out.append(_row("mem-ok", "Memory", f"{len(files)} memories, synced", "clean",
                        "info", "robot reachable, cache mirrors it", "",
                        "memories/*.md counted locally; robot link is up"))
    return out


def _check_cloud() -> list[dict]:
    try:
        import reachy_supermemory as sm
        ok, space = sm.available(), sm.SPACE
    except Exception as e:  # noqa: BLE001
        return [_row("cloud", "Memory", "Cannot tell if the cloud layer is on",
                     "unknown", "info", str(e)[:140], "",
                     "import reachy_supermemory")]
    if ok:
        return [_row("cloud", "Memory", f"Cloud layer on (space {space})", "clean",
                     "info", "an API key is configured", "",
                     "reachy_supermemory.available()")]
    return [_row("cloud", "Memory", "Cloud memory layer is off", "gap", "polish",
                 "no Supermemory key, so the third layer holds nothing",
                 "robot and Mac layers still work; only long-term recall is missing",
                 "reachy_supermemory.available() is False",
                 {"kind": "none", "label": "needs SUPERMEMORY_API_KEY in .env"})]


def _check_faces() -> list[dict]:
    out = []
    try:
        import reachy_faces_store as fs
        faces = fs.faces_for_prune()
        samples = {f["id"]: fs.count_samples(f["id"]) for f in faces}
    except Exception as e:  # noqa: BLE001
        return [_row("faces", "Faces", "Cannot read the face database", "unknown",
                     "info", str(e)[:140], "",
                     "reachy_faces_store.faces_for_prune()")]
    if not faces:
        return [_row("faces-none", "Faces", "Nobody has been taught yet", "gap",
                     "degraded", "the faces table is empty",
                     "Vibey greets everyone as a stranger, including Jack",
                     "faces_for_prune() returned 0 rows",
                     {"kind": "jump", "label": "Open Friends", "target": "gallery"})]

    unnamed = [f for f in faces if not (f.get("name") or "").strip()]
    if unnamed:
        out.append(_row("faces-unnamed", "Faces",
                        f"{len(unnamed)} face(s) seen but never named", "gap",
                        "degraded",
                        "recognised every time, greeted by nobody's name",
                        "a nameless row is the single cheapest gap on this page to close",
                        "faces rows with an empty name column",
                        {"kind": "jump", "label": "Name them", "target": "gallery"}))
    weak = [f for f in faces
            if (f.get("name") or "").strip() and samples.get(f["id"], 0) < WEAK_SAMPLES]
    if weak:
        names = ", ".join(sorted((f["name"] or "?") for f in weak))[:90]
        out.append(_row("faces-weak", "Faces",
                        f"{len(weak)} friend(s) with thin face data", "gap",
                        "polish", f"under {WEAK_SAMPLES} samples each: {names}",
                        "one angle is one lighting condition; new light reads as a stranger",
                        f"count_samples() < {WEAK_SAMPLES} per named face",
                        {"kind": "jump", "label": "Open Friends", "target": "gallery"}))
    if not out:
        out.append(_row("faces-ok", "Faces", f"{len(faces)} friends, all named",
                        "clean", "info",
                        f"every one has {WEAK_SAMPLES}+ samples", "",
                        "faces_for_prune() + count_samples()"))
    return out


def _check_journal() -> list[dict]:
    out = []
    latest_t, days = _newest("transcripts", ".jsonl")
    if latest_t and latest_t[:10] == _today():
        out.append(_row("tx", "Journal", "Today is being transcribed", "clean",
                        "info", f"{days} day(s) on disk", "",
                        "newest transcripts/*.jsonl is dated today"))
    else:
        out.append(_row("tx", "Journal", "Nothing transcribed today", "gap",
                        "polish",
                        f"newest transcript is {latest_t[:10] if latest_t else 'none'}",
                        "no transcript means today leaves no trace to summarise later",
                        "newest transcripts/*.jsonl is not dated today",
                        {"kind": "jump", "label": "Talk to Vibey", "target": "convo"}))

    latest_n, notes = _newest("notes", ".md")
    age = _days_old(latest_n) if latest_n else None
    if latest_n and age is not None and age <= STALE_NOTE_DAYS:
        out.append(_row("notes", "Journal", "Notes are current", "clean", "info",
                        f"{notes} note(s), newest {age} day(s) old", "",
                        "newest notes/*.md filename date"))
    else:
        out.append(_row("notes", "Journal", "Notes have gone stale", "gap", "polish",
                        f"newest note is {latest_n[:10] if latest_n else 'none'}",
                        "the notes folder is what a demo reads from when asked what happened",
                        f"newest notes/*.md is older than {STALE_NOTE_DAYS} days",
                        {"kind": "none", "label": "write one in notes/"}))
    return out


def _check_identity() -> list[dict]:
    out = []
    for fname, area, why in (
        ("SKILLS.md", "Identity", "the realtime brain is handed this file at connect time"),
        ("IDENTITY.md", "Identity", "without it Vibey introduces themself as a generic assistant"),
    ):
        p = HERE / fname
        body = p.read_text(encoding="utf-8").strip() if p.is_file() else ""
        if body:
            out.append(_row(fname, area, f"{fname} present", "clean", "info",
                            f"{len(body.splitlines())} lines", "",
                            f"read {fname}"))
        else:
            out.append(_row(fname, area, f"{fname} is missing or empty", "gap",
                            "degraded", "nothing to load into the prompt", why,
                            f"read {fname} returned nothing",
                            {"kind": "none", "label": f"write {fname}"}))
    return out


# ----------------------------------------------------------------------- scan
def scan(force: bool = False) -> dict:
    """Every check, gaps and clean alike, plus the counts the header needs."""
    global _CACHE
    if _CACHE and not force and time.time() - _CACHE[0] < _TTL:
        return _CACHE[1]

    link = _check_link()
    robot_up = link[0]["state"] == "clean"
    rows = (link + _check_services() + _check_memories(robot_up) + _check_cloud()
            + _check_faces() + _check_journal() + _check_identity())

    gaps = [r for r in rows if r["state"] == "gap"]
    out = {
        "at": time.time(),
        "rows": rows,
        "counts": {
            "checks": len(rows),
            "clean": sum(1 for r in rows if r["state"] == "clean"),
            "unknown": sum(1 for r in rows if r["state"] == "unknown"),
            "gaps": len(gaps),
            "blocker": sum(1 for r in gaps if r["severity"] == "blocker"),
            "degraded": sum(1 for r in gaps if r["severity"] == "degraded"),
            "polish": sum(1 for r in gaps if r["severity"] == "polish"),
        },
    }
    _CACHE = (time.time(), out)
    return out


if __name__ == "__main__":
    d = scan(force=True)
    c = d["counts"]
    print(f"{c['gaps']} gap(s) of {c['checks']} checks — "
          f"{c['blocker']} blocker, {c['degraded']} degraded, {c['polish']} polish"
          + (f", {c['unknown']} unknown" if c["unknown"] else ""))
    for r in d["rows"]:
        if r["state"] != "clean":
            print(f"  [{r['severity']:8}] {r['area']:11} {r['title']}")
            print(f"             {r['detail']}  ·  basis: {r['basis']}")
