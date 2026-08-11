"""Persistent done-state (scheme B)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from folder_watch.support.log import _jsonable
from folder_watch.support.paths import STATE_DIR


class DoneStore:
    """Persistent done records so restarts don't re-copy the same work."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def path_for(action: str, src: Path, *, to: str | None = None, mode: str | None = None) -> Path:
        raw = f"{action}|{src.expanduser().resolve()}|{to or ''}|{mode or ''}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return STATE_DIR / f"done-{action}-{digest}.json"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            self._data = data["entries"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": self._data}
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix="done-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def make_key(
        action: str,
        src: Path,
        *,
        size: int,
        mtime_ns: int,
        dest: Path | None = None,
        mode: str | None = None,
    ) -> str:
        dest_s = str(dest.expanduser().resolve()) if dest else ""
        src_s = str(src.expanduser().resolve())
        return f"{action}|{src_s}|{dest_s}|{mode or ''}|{size}|{mtime_ns}"

    def is_done(self, key: str) -> bool:
        return key in self._data

    def mark(self, key: str, **meta: Any) -> None:
        self._data[key] = {"ts": time.time(), **{k: _jsonable(v) for k, v in meta.items()}}
        self.save()

    def mark_baseline_file(
        self,
        action: str,
        src: Path,
        *,
        size: int,
        mtime_ns: int,
        dest: Path | None = None,
        mode: str | None = None,
    ) -> str:
        key = self.make_key(
            action, src, size=size, mtime_ns=mtime_ns, dest=dest, mode=mode
        )
        if not self.is_done(key):
            self.mark(key, baseline=True, name=src.name)
        return key


def file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
