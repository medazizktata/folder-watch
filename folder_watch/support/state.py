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
        # Keys this process removed on purpose, so a merge doesn't resurrect them.
        self._dropped: set[str] = set()
        # (size, mtime_ns) of the file as we last left it. If it still matches,
        # nobody else has written and the merge read can be skipped entirely.
        self._last_write: tuple[int, int] | None = None
        self._load()

    @staticmethod
    def path_for(
        action: str,
        src: Path,
        *,
        to: str | None = None,
        mode: str | None = None,
    ) -> Path:
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

    def _touched_by_someone_else(self) -> bool:
        """Has the file changed since we last wrote it?"""
        try:
            st = self.path.stat()
        except OSError:
            return self._last_write is not None
        return self._last_write != (st.st_size, st.st_mtime_ns)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Merge whatever is on disk first, so a second watcher (or a --pending
        # run) can't erase this process's entries by writing its own snapshot.
        # Only when the file actually changed under us, though: re-reading and
        # re-parsing on every mark() made a growing store quadratic, which is
        # the common single-writer case and pure waste.
        if self._touched_by_someone_else():
            on_disk: dict[str, dict[str, Any]] = {}
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                    on_disk = data["entries"]
            except (OSError, json.JSONDecodeError):
                on_disk = {}
            merged = {**on_disk, **self._data}
            for key in set(on_disk) - set(self._data):
                if key in self._dropped:
                    merged.pop(key, None)
            self._data = merged
        payload = {"version": 1, "entries": self._data}
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(
            prefix="done-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            try:
                st = self.path.stat()
                self._last_write = (st.st_size, st.st_mtime_ns)
            except OSError:
                self._last_write = None
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
        self._data[key] = {
            "ts": time.time(),
            **{k: _jsonable(v) for k, v in meta.items()},
        }
        self.save()

    @staticmethod
    def key_src(key: str) -> str | None:
        """Source path out of a key: action|src|dest|mode|size|mtime.

        Only when that split is unambiguous. A "|" anywhere in the source or
        destination makes the field count wrong, and guessing produced paths
        like "/a/src.mp4|/dest" that exist nowhere — so prune classed a live
        entry as dead and deleted it. Entries written since carry an explicit
        "src", so this is only a fallback for older ones.
        """
        parts = key.split("|")
        if len(parts) != 6:
            return None
        return parts[1]

    def entry_src(self, key: str) -> str | None:
        """Prefer the recorded source; fall back to parsing older entries."""
        recorded = (self._data.get(key) or {}).get("src")
        if isinstance(recorded, str) and recorded:
            return recorded
        return self.key_src(key)

    def dead_keys(self) -> list[str]:
        """Entries whose source file is gone.

        Entries are keyed by size+mtime, so once a file is deleted or rewritten
        its old entry can never match again — it is pure dead weight.
        """
        dead: list[str] = []
        for key in self._data:
            src = self.entry_src(key)
            if src is None:
                continue
            try:
                if not Path(src).exists():
                    dead.append(key)
            except OSError:
                continue
        return dead

    def drop_baseline(self) -> int:
        """Forget entries that were only ever assumed done, not verified.

        mark_baseline_file records "this was sitting here when I started, so
        presume it's handled". --catch-up is the user saying that presumption
        was wrong, so it has to clear them or the catch-up scan skips exactly
        the files it was asked to process.
        """
        dead = [k for k, v in self._data.items() if v.get("baseline")]
        for key in dead:
            self._data.pop(key, None)
            self._dropped.add(key)
        if dead:
            self.save()
        return len(dead)

    def prune(self) -> int:
        """Drop dead entries. Returns how many went."""
        dead = self.dead_keys()
        for key in dead:
            self._data.pop(key, None)
            self._dropped.add(key)
        if dead:
            self.save()
        return len(dead)

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        """A store is a store even when empty.

        Without this, __len__ makes a fresh store falsy, and `if done_store:`
        silently reads as "no done-store configured".
        """
        return True

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
            self.mark(key, baseline=True, name=src.name, src=str(src))
        return key


def file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
