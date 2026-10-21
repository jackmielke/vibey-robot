"""Safe writes for the little JSON state files several processes share.

`Path.write_text` truncates the file first and writes second, so a reader that
lands in between sees an empty or half-written file. Most readers treat a parse
error as "nothing there yet" and the next save then wipes the real contents
(every alarm, or the Telegram owner pairing). Writing a temp file next to the
target and renaming it over the top is atomic on the same filesystem: readers
see either the old file or the new one, never a torn one.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | os.PathLike, text: str) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)   # keep e.g. 600
        except OSError:
            os.chmod(tmp, 0o644)   # new file: mkstemp's 600 would be a surprise
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike, obj, **dump_kwargs) -> None:
    atomic_write_text(path, json.dumps(obj, **dump_kwargs))
