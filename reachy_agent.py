#!/usr/bin/env python3
"""
reachy_agent.py — Vibey's "go think about it" button.

The realtime voice brain (reachy_openai_realtime.py) is fast and charming but
deliberately dumb about code. When Jack says "you should be able to wave when I
wave at you", that's not a reply — it's a work order. This module is how the
conversation hands that work order to a *real* coding agent (the Claude CLI,
pointed at this very repo) without the conversation stopping to wait.

    voice: "can you learn to wave back?"
      └─▶ dispatch("teach yourself to wave back when someone waves")
            └─▶ claude -p …  (background thread, cwd=this repo)
      voice keeps talking the entire time
      later: "how'd that go?"  └─▶ status() → spoken summary

Every job records the git SHA it started from, so any run is one command away
from being undone (see `undo_hint`). Output is teed to agent_runs/ so you can
read the whole transcript afterwards instead of trusting a one-line summary.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

REPO = Path(__file__).parent.resolve()
RUNS_DIR = REPO / "agent_runs"

# Where the coding agent's permissions land. "acceptEdits" lets it write files
# but still gates shell commands; "bypassPermissions" is full autonomy. Vibey
# editing their own body is the point, so this is deliberately configurable and
# deliberately NOT bypass-by-default.
PERMISSION_MODE = os.environ.get("AGENT_PERMISSION_MODE", "acceptEdits").strip()
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5").strip()
AGENT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "900"))  # seconds

# Prepended to every task so the agent knows whose body it's editing.
BRIEFING = """You are the coding agent behind Vibey, a Reachy Mini desk robot.
You are editing Vibey's OWN source code, live, while Jack is talking to them
through the OpenAI Realtime voice brain (reachy_openai_realtime.py).

Repo layout that matters:
  reachy_openai_realtime.py  the realtime voice brain + its tool definitions
  reachy_emotes.py           head/antenna choreographies (_MOVES registry)
  reachy_voice.py            ElevenLabs TTS -> robot speaker helpers
  reachy_chat.py             the multi-brain chat service + dashboard state
  reachy_agent.py            this dispatcher (you are running inside it)
  SKILLS.md                  things Vibey has been taught in conversation

Rules for this run:
  * Make the SMALLEST change that actually accomplishes the task.
  * New robot motions go in reachy_emotes.py and get registered in _MOVES so
    the voice brain can call them by name immediately.
  * Never edit .env, never commit, never push, never restart services.
  * Prefer stdlib. The chat venv is lean (numpy/sounddevice/websockets).
  * End your reply with ONE short sentence, written to be read aloud by Vibey
    in first person — e.g. "I taught myself to wave, try it." That sentence is
    literally what they say next, so no file paths or code in it.
"""


def _resolve_claude_bin() -> str:
    """Absolute path to the claude CLI. Bare "claude" only resolves if PATH
    happened to include it at process-start — flaky when this is launched by a
    service rather than a shell (same trap reachy_chat.py works around)."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ):
        if os.path.isfile(candidate):
            return candidate
    return "claude"


CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or _resolve_claude_bin()

# job_id -> {state, task, started, finished, sha, reply, log, error}
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def available() -> bool:
    """True if we can actually shell out to a coding agent."""
    return bool(shutil.which(CLAUDE_BIN) or os.path.isfile(CLAUDE_BIN))


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _spoken_tail(reply: str) -> str:
    """The agent is briefed to end with one read-aloud sentence. Take the last
    non-empty line, but fall back to something sane if it ignored that."""
    lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
    if not lines:
        return "I finished, but I didn't have anything to say about it."
    tail = lines[-1].lstrip("#*-> ").strip()
    # A last line that's obviously code//a path isn't speakable.
    if len(tail) > 300 or tail.startswith(("```", "/", "$")):
        return "I finished that one — check the log for what I changed."
    return tail


def _run_job(job_id: str, task: str, on_done) -> None:
    job = JOBS[job_id]
    log_path = RUNS_DIR / f"{job_id}.log"
    prompt = f"{BRIEFING}\n\nTASK FROM JACK (spoken out loud, transcribed):\n{task}\n"

    cmd = [CLAUDE_BIN, "-p", prompt,
           "--permission-mode", PERMISSION_MODE,
           "--model", AGENT_MODEL]
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=AGENT_TIMEOUT)
        reply = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        ok = proc.returncode == 0
        with _LOCK:
            job["state"] = "done" if ok else "failed"
            job["reply"] = reply
            job["error"] = "" if ok else (err or f"exit {proc.returncode}")
            job["spoken"] = (_spoken_tail(reply) if ok
                             else "That didn't work — something went wrong on my end.")
    except subprocess.TimeoutExpired:
        with _LOCK:
            job["state"] = "failed"
            job["error"] = f"timed out after {AGENT_TIMEOUT:.0f}s"
            job["spoken"] = "I ran out of time on that one."
        reply, err = "", job["error"]
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            job["state"] = "failed"
            job["error"] = str(e)
            job["spoken"] = "I couldn't start that — my coding agent didn't come up."
        reply, err = "", str(e)

    with _LOCK:
        job["finished"] = time.time()

    try:
        RUNS_DIR.mkdir(exist_ok=True)
        log_path.write_text(
            f"# job {job_id}\n# task: {task}\n# started at git {job['sha']}\n"
            f"# state: {job['state']}\n\n{reply}\n\n--- stderr ---\n{err}\n")
        with _LOCK:
            job["log"] = str(log_path)
    except Exception:  # noqa: BLE001
        pass

    print(f"[agent] job {job_id} {job['state']} "
          f"({job['finished'] - job['started']:.0f}s): {job.get('spoken','')}",
          flush=True)

    if on_done:
        try:
            on_done(dict(job))
        except Exception as e:  # noqa: BLE001
            print(f"[agent] on_done callback failed: {e}", flush=True)


def dispatch(task: str, on_done=None) -> dict:
    """Kick off a coding agent in the background. Returns immediately with the
    job record — the caller (a voice turn) must not block on this."""
    if not available():
        return {"id": None, "state": "failed",
                "error": f"claude CLI not found at {CLAUDE_BIN}",
                "spoken": "I can't reach my coding agent right now."}
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "state": "running", "task": task,
           "started": time.time(), "finished": None, "sha": _git_sha(),
           "reply": "", "error": "", "spoken": "", "log": ""}
    with _LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_run_job, args=(job_id, task, on_done),
                     daemon=True).start()
    print(f"[agent] job {job_id} started from git {job['sha']}: {task[:80]}",
          flush=True)
    return dict(job)


def status(job_id: str | None = None) -> dict:
    """Status of one job, or of the most recent one if job_id is omitted."""
    with _LOCK:
        if job_id:
            job = JOBS.get(job_id)
        else:
            job = max(JOBS.values(), key=lambda j: j["started"], default=None)
        if not job:
            return {"state": "none", "spoken": "I haven't worked on anything yet."}
        snap = dict(job)
    if snap["state"] == "running":
        secs = time.time() - snap["started"]
        snap["spoken"] = (f"Still working on it — about {secs:.0f} seconds in.")
    return snap


def undo_hint(job_id: str) -> str:
    """The exact command to rewind the repo to before a job ran."""
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        return ""
    return f"git -C {REPO} diff {job['sha']} -- . && git -C {REPO} checkout {job['sha']} -- ."


# --------------------------------------------------------------------------- #
# Skills — the cheap half of "learn while we talk".
# Not every lesson deserves a whole coding agent. "You can call me Jack" or
# "keep your answers shorter at night" is just a note, and notes get folded
# into the realtime session's instructions on the next reconnect.
# --------------------------------------------------------------------------- #
SKILLS_FILE = REPO / "SKILLS.md"
SKILLS_HEADER = """# What Vibey has been taught

Appended to by the `remember` voice tool during conversations, and loaded into
the realtime brain's instructions at connect time. Hand-editable — trim freely.
"""


def remember(note: str) -> str:
    """Append a lesson learned in conversation. Returns the note stored."""
    note = " ".join(note.split()).strip()
    if not note:
        return ""
    stamp = time.strftime("%Y-%m-%d")
    if not SKILLS_FILE.exists():
        SKILLS_FILE.write_text(SKILLS_HEADER)
    with SKILLS_FILE.open("a") as f:
        f.write(f"- ({stamp}) {note}\n")
    print(f"[agent] remembered: {note}", flush=True)
    return note


def load_skills(limit: int = 40) -> str:
    """The most recent lessons, as a block to paste into the voice instructions."""
    if not SKILLS_FILE.exists():
        return ""
    lines = [ln.strip() for ln in SKILLS_FILE.read_text().splitlines()
             if ln.strip().startswith("- ")]
    if not lines:
        return ""
    return "\n".join(lines[-limit:])


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <task for the coding agent>")
        print(f"claude bin: {CLAUDE_BIN} (available={available()})")
        sys.exit(1)
    j = dispatch(" ".join(sys.argv[1:]))
    print(json.dumps(j, indent=2, default=str))
    while status(j["id"])["state"] == "running":
        time.sleep(3)
    final = status(j["id"])
    print(f"\nstate: {final['state']}\nspoken: {final['spoken']}\nlog: {final['log']}")
