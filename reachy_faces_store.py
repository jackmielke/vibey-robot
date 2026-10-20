"""Vibey's friends and faces, stored locally: SQLite + JPEG files on this Mac.

This is the ONLY module that knows where faces live. reachy_memory.py (the
:8773 service) and the dashboard go through the functions below. Nothing here
talks to the network.

    data/vibey.db             faces, face_samples, journal
    data/snapshots/<id>.jpg   one JPEG per face / sample (the old base64 data URIs)

data/ is gitignored and chmod 700. Until 2026-09-30 all of this lived in the
Supabase tables faces / face_samples / vibey_journal_entries; see
migrate_faces_from_supabase.py for the one-time copy.

Snapshots go in and come out as `data:image/jpeg;base64,...` strings so every
caller (and the dashboard's <img src>) keeps working unchanged; only the
storage underneath is files.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(os.environ.get("VIBEY_DATA_DIR")
            or Path(__file__).resolve().parent / "data")
DB_PATH = DATA / "vibey.db"
SNAPS = DATA / "snapshots"
_PREFIX = "data:image/jpeg;base64,"

_SCHEMA = """
create table if not exists faces (
    id          text primary key,
    name        text,
    times_seen  integer not null default 1,
    first_seen  text not null,
    last_seen   text not null,
    notes       text,
    snapshot    text               -- file name under data/snapshots, or null
);
create table if not exists face_samples (
    id          text primary key,
    face_id     text not null references faces(id) on delete cascade,
    embedding   text not null,     -- JSON list of 128 floats
    snapshot    text,
    created_at  text not null
);
create index if not exists face_samples_face on face_samples(face_id, created_at);
create table if not exists journal (
    id              text primary key,
    community_id    text,
    body            text not null,
    mood            text,
    source_summary  text,
    reflection_seed text,
    message_count   integer,
    created_at      text not null
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _db():
    if not DATA.exists():
        DATA.mkdir(parents=True)
        os.chmod(DATA, 0o700)
    SNAPS.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("pragma foreign_keys = on")
    c.execute("pragma journal_mode = wal")
    c.executescript(_SCHEMA)
    try:
        yield c
    finally:
        c.close()


# ----------------------------------------------------------------- snapshots
def _save_snap(key: str, data_uri: str | None) -> str | None:
    if not data_uri:
        return None
    raw = data_uri.split(",", 1)[1] if data_uri.startswith("data:") else data_uri
    try:
        jpg = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        return None
    fn = f"{key}.jpg"
    (SNAPS / fn).write_bytes(jpg)
    return fn


def _load_snap(fn: str | None) -> str | None:
    if not fn:
        return None
    try:
        return _PREFIX + base64.b64encode((SNAPS / fn).read_bytes()).decode()
    except OSError:
        return None


def _drop_snap(fn: str | None) -> None:
    if fn:
        try:
            (SNAPS / fn).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------- faces
def get_faces() -> list[dict]:
    """Everyone Vibey knows: id, name, times_seen, snapshot (data URI)."""
    with _db() as c:
        rows = c.execute("select id, name, times_seen, snapshot from faces").fetchall()
    return [dict(r, snapshot=_load_snap(r["snapshot"])) for r in rows]


def faces_for_prune() -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute(
            "select id, name, times_seen, last_seen from faces")]


def get_samples() -> list[dict]:
    """Every embedding sample tagged with its person — the matching model.
    Same shape the Supabase version returned."""
    with _db() as c:
        rows = c.execute(
            "select s.id, s.face_id, s.embedding, s.created_at, f.name, f.times_seen "
            "from face_samples s join faces f on f.id = s.face_id").fetchall()
    return [{"sample_id": r["id"], "face_id": r["face_id"],
             "embedding": json.loads(r["embedding"]), "created_at": r["created_at"],
             "name": r["name"], "times_seen": r["times_seen"]} for r in rows]


def insert_face(snapshot: str | None) -> dict:
    fid, now = str(uuid.uuid4()), _now()
    with _db() as c:
        c.execute("insert into faces (id, times_seen, first_seen, last_seen, snapshot) "
                  "values (?, 1, ?, ?, ?)", (fid, now, now, _save_snap(f"f_{fid}", snapshot)))
    return {"id": fid, "name": None, "times_seen": 1}


def _trim(c: sqlite3.Connection, face_id: str, keep: int) -> None:
    rows = c.execute("select id, snapshot from face_samples where face_id = ? "
                     "order by created_at asc", (face_id,)).fetchall()
    for r in rows[:max(0, len(rows) - keep)]:
        c.execute("delete from face_samples where id = ?", (r["id"],))
        _drop_snap(r["snapshot"])


def add_sample(face_id: str, embedding: list[float], snapshot: str | None,
               max_samples: int = 8) -> None:
    """Bank a sample, dropping the oldest so a person keeps at most max_samples."""
    sid = str(uuid.uuid4())
    with _db() as c:
        _trim(c, face_id, max_samples - 1)
        c.execute("insert into face_samples (id, face_id, embedding, snapshot, created_at) "
                  "values (?, ?, ?, ?, ?)",
                  (sid, face_id, json.dumps(list(map(float, embedding))),
                   _save_snap(f"s_{sid}", snapshot), _now()))


def touch_face(face_id: str, times_seen: int) -> None:
    with _db() as c:
        c.execute("update faces set times_seen = ?, last_seen = ? where id = ?",
                  (times_seen + 1, _now(), face_id))


def name_face(face_id: str, name: str) -> None:
    with _db() as c:
        c.execute("update faces set name = ? where id = ?", (name, face_id))


def delete_face(face_id: str) -> None:
    """Forget a person entirely, samples and photos included."""
    with _db() as c:
        snaps = [r[0] for r in c.execute(
            "select snapshot from face_samples where face_id = ?", (face_id,))]
        snaps += [r[0] for r in c.execute(
            "select snapshot from faces where id = ?", (face_id,))]
        c.execute("delete from face_samples where face_id = ?", (face_id,))
        c.execute("delete from faces where id = ?", (face_id,))
    for s in snaps:
        _drop_snap(s)


def find_face_by_name(name: str) -> dict | None:
    with _db() as c:
        r = c.execute("select id, name, times_seen from faces where name = ? limit 1",
                      (name,)).fetchone()
    return dict(r) if r else None


def merge_faces(src_id: str, dst_id: str, max_samples: int = 8) -> None:
    """Fold src into dst: samples move, sighting counts add, src disappears."""
    with _db() as c:
        c.execute("update face_samples set face_id = ? where face_id = ?", (dst_id, src_id))
        c.execute("update faces set times_seen = times_seen + coalesce("
                  "(select times_seen from faces where id = ?), 0) where id = ?",
                  (src_id, dst_id))
        _trim(c, dst_id, max_samples)
    delete_face(src_id)


# ------------------------------------------------------------------- samples
def samples_for(face_id: str) -> list[dict]:
    """All photos of one person, newest first: [{id, snapshot, created_at}]."""
    with _db() as c:
        rows = c.execute("select id, snapshot, created_at from face_samples "
                         "where face_id = ? order by created_at desc", (face_id,)).fetchall()
    return [dict(r, snapshot=_load_snap(r["snapshot"])) for r in rows]


def all_sample_photos() -> list[dict]:
    """[{face_id, snapshot, created_at}] newest first, for the Friends panel."""
    with _db() as c:
        rows = c.execute("select face_id, snapshot, created_at from face_samples "
                         "order by created_at desc").fetchall()
    return [dict(r, snapshot=_load_snap(r["snapshot"])) for r in rows]


def sample_face(sample_id: str) -> str | None:
    with _db() as c:
        r = c.execute("select face_id from face_samples where id = ?", (sample_id,)).fetchone()
    return r[0] if r else None


def count_samples(face_id: str) -> int:
    with _db() as c:
        return c.execute("select count(*) from face_samples where face_id = ?",
                         (face_id,)).fetchone()[0]


def delete_sample(sample_id: str) -> None:
    with _db() as c:
        r = c.execute("select snapshot from face_samples where id = ?", (sample_id,)).fetchone()
        c.execute("delete from face_samples where id = ?", (sample_id,))
    if r:
        _drop_snap(r[0])


def get_face(face_id: str) -> dict | None:
    """One person for the phone's profile: basics plus every photo (sample),
    newest first, each with its own id so it can be deleted or moved."""
    with _db() as c:
        r = c.execute("select id, name, times_seen, first_seen, last_seen, snapshot "
                      "from faces where id = ?", (face_id,)).fetchone()
    if not r:
        return None
    return dict(r, snapshot=_load_snap(r["snapshot"]), samples=samples_for(face_id))


def find_face_by_name_ci(name: str) -> dict | None:
    """Like find_face_by_name, but "jack" finds Jack: typing a name on a phone
    should not quietly start a second Jack."""
    with _db() as c:
        r = c.execute("select id, name, times_seen from faces where lower(name) = lower(?) "
                      "order by times_seen desc limit 1", (name.strip(),)).fetchone()
    return dict(r) if r else None


def sample_snapshot(sample_id: str) -> str | None:
    with _db() as c:
        r = c.execute("select snapshot from face_samples where id = ?", (sample_id,)).fetchone()
    return _load_snap(r[0]) if r else None


def move_sample(sample_id: str, face_id: str) -> None:
    """Re-file one photo (and its embedding) under another person. The
    embedding moves with it, so recognition learns from the correction."""
    with _db() as c:
        c.execute("update face_samples set face_id = ? where id = ?", (face_id, sample_id))


# ------------------------------------------------------------------- journal
def add_journal(body: str, message_count: int = 0, source_summary: str = "reachy-robot",
                community_id: str | None = None) -> None:
    with _db() as c:
        c.execute("insert into journal (id, community_id, body, source_summary, "
                  "message_count, created_at) values (?, ?, ?, ?, ?, ?)",
                  (str(uuid.uuid4()), community_id, body, source_summary,
                   message_count, _now()))


def recent_journal(limit: int = 2) -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute(
            "select body, created_at from journal order by created_at desc limit ?",
            (limit,))]


def counts() -> dict:
    with _db() as c:
        return {t: c.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ("faces", "face_samples", "journal")}


# ------------------------------------------------------------------- import
def import_rows(faces: list[dict], samples: list[dict], journal: list[dict]) -> None:
    """Idempotent bulk load, keeping the original ids and timestamps."""
    with _db() as c:
        c.execute("begin")
        for f in faces:
            c.execute("insert or replace into faces (id, name, times_seen, first_seen, "
                      "last_seen, notes, snapshot) values (?, ?, ?, ?, ?, ?, ?)",
                      (f["id"], f.get("name"), f.get("times_seen") or 1,
                       f.get("first_seen") or _now(), f.get("last_seen") or _now(),
                       f.get("notes"), _save_snap(f"f_{f['id']}", f.get("snapshot"))))
        for s in samples:
            c.execute("insert or replace into face_samples (id, face_id, embedding, "
                      "snapshot, created_at) values (?, ?, ?, ?, ?)",
                      (s["id"], s["face_id"], json.dumps(s["embedding"]),
                       _save_snap(f"s_{s['id']}", s.get("snapshot")),
                       s.get("created_at") or _now()))
        for j in journal:
            seed = j.get("reflection_seed")
            c.execute("insert or replace into journal (id, community_id, body, mood, "
                      "source_summary, reflection_seed, message_count, created_at) "
                      "values (?, ?, ?, ?, ?, ?, ?, ?)",
                      (j["id"], j.get("community_id"), j.get("body") or "", j.get("mood"),
                       j.get("source_summary"), json.dumps(seed) if seed is not None else None,
                       j.get("message_count"), j.get("created_at") or _now()))
        # Mirror: drop anything the source no longer has (the old service kept
        # trimming samples while this ran), then any photo nothing points at.
        for table, rows in (("face_samples", samples), ("faces", faces)):
            keep = {r["id"] for r in rows}
            for (rid,) in c.execute(f"select id from {table}").fetchall():
                if rid not in keep:
                    c.execute(f"delete from {table} where id = ?", (rid,))
        c.execute("commit")
        used = {r[0] for r in c.execute(
            "select snapshot from faces union select snapshot from face_samples") if r[0]}
    for p in SNAPS.glob("*.jpg"):
        if p.name not in used:
            p.unlink()
