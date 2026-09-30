"""One-time copy of Vibey's faces off Supabase into data/vibey.db.

READ ONLY against Supabase: plain GETs, nothing is deleted or altered there.
Safe to re-run (rows are upserted by their original id).

    set -a; source .env; set +a; reachy_env/bin/python migrate_faces_from_supabase.py

Journal: only the robot's own rows (source_summary=reachy-robot) are copied.
The rest of vibey_journal_entries is the Vibe-world diary and stays put.
"""
import json
import os
import sys
import urllib.request

import reachy_faces_store as store

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_KEY"]
PAGE = 500


def fetch(table: str, query: str) -> tuple[list[dict], int]:
    rows, start, total = [], 0, None
    while True:
        req = urllib.request.Request(
            f"{URL}/rest/v1/{table}?{query}&order=id.asc",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                     "Prefer": "count=exact", "Range": f"{start}-{start + PAGE - 1}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers["Content-Range"].split("/")[-1])
            page = json.loads(r.read() or b"[]")
        rows += page
        start += PAGE
        if not page or start >= total:
            return rows, total


def main() -> int:
    faces, nf = fetch("faces", "select=*")
    samples, ns = fetch("face_samples", "select=*")
    journal, nj = fetch("vibey_journal_entries", "select=*&source_summary=eq.reachy-robot")
    print(f"supabase: faces={nf} face_samples={ns} journal(reachy-robot)={nj}")
    assert (len(faces), len(samples), len(journal)) == (nf, ns, nj), "short read"
    store.import_rows(faces, samples, journal)
    os.chmod(store.DATA, 0o700)
    local = store.counts()
    print(f"local:    {local}")
    ok = local == {"faces": nf, "face_samples": ns, "journal": nj}
    ok = ok and {r["sample_id"] for r in store.get_samples()} == {r["id"] for r in samples}
    snaps = len(list(store.SNAPS.glob("*.jpg")))
    want = sum(1 for r in faces + samples if r.get("snapshot"))
    print(f"snapshots on disk: {snaps} (expected {want})")
    return 0 if ok and snaps == want else 1


if __name__ == "__main__":
    sys.exit(main())
