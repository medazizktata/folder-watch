"""Retry queue for transient action failures.

On the event backend the only thing that re-queues a file is a new FSEvent on
the *source*. A destination coming back produces no source event, so a copy
that failed because the drive was unmounted was never retried — it just sat
there, correctly not marked done, waiting for someone to notice.

Design notes worth keeping:

* Retries deliberately bypass the settle loop. A queued file already settled
  once — that is why the action ran — so re-settling would pay another `lsof`
  (~250ms) per file per attempt. Instead the (size, mtime) at failure time is
  recorded; if it still matches, the action is applied directly. If it moved,
  the file was rewritten and belongs back in the normal settle path.
* Timestamps are wall-clock, not monotonic, because the queue is persisted and
  monotonic clocks do not survive a restart. Sleep time therefore counts
  against the give-up budget, which is exactly why that budget is measured in
  days rather than hours: shut the lid on Friday with the drive unmounted and
  ~60 hours pass before the watcher runs again.
* Expiry is not data loss. The source is untouched, nothing was marked done,
  --pending still reports it and --catch-up still picks it up. It only means
  "stopped retrying on its own".
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from folder_watch.support.paths import STATE_DIR

# A long outage costs a handful of attempts per hour, not thousands.
BACKOFF = (30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)

# Long enough to cover a weekend with the machine asleep. See module docstring.
GIVE_UP_AFTER = 72 * 3600.0

# Bounds so a wholly dead destination can't grow the queue without limit, and
# so a large backlog coming back doesn't saturate the destination in one burst.
MAX_ENTRIES = 1000
MAX_PER_SCAN = 20


class RetryQueue:
    def __init__(
        self,
        path: Path,
        *,
        give_up_after: float = GIVE_UP_AFTER,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.path = path.expanduser()
        self.give_up_after = give_up_after
        self.max_entries = max_entries
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def path_for(done_store_path: Path) -> Path:
        """Sit beside the done-store for the same watch."""
        name = done_store_path.name.replace("done-", "retry-", 1)
        return done_store_path.with_name(name)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            self._data = {
                k: v for k, v in data["entries"].items() if isinstance(v, dict)
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": self._data}
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(
            prefix="retry-", suffix=".json", dir=str(self.path.parent)
        )
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

    # -- queue operations ---------------------------------------------------

    def schedule(
        self,
        src: Path,
        *,
        action: str,
        reason: str,
        size: int,
        mtime_ns: int,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Queue a transient failure. Returns the entry, or None if full."""
        now = time.time() if now is None else now
        key = str(src)
        prev = self._data.get(key)
        attempts = int(prev.get("attempts", 0)) + 1 if prev else 1
        if prev is None and len(self._data) >= self.max_entries:
            return None
        entry = {
            "path": key,
            "action": action,
            "reason": reason,
            "attempts": attempts,
            "first_failed": prev.get("first_failed", now) if prev else now,
            "next_due": now + BACKOFF[min(attempts - 1, len(BACKOFF) - 1)],
            "size": size,
            "mtime_ns": mtime_ns,
        }
        self._data[key] = entry
        self.save()
        return entry

    def drop(self, src: Path) -> bool:
        if self._data.pop(str(src), None) is None:
            return False
        self.save()
        return True

    def due(self, now: float | None = None, *, limit: int = MAX_PER_SCAN) -> list[dict]:
        now = time.time() if now is None else now
        ready = [e for e in self._data.values() if e.get("next_due", 0) <= now]
        ready.sort(key=lambda e: e.get("next_due", 0))
        return ready[:limit]

    def defer(self, entries: list[dict], now: float | None = None) -> None:
        """Push entries to their next backoff without counting an attempt.

        Used when the destination is known to still be away: attempting each
        file individually would produce N identical failures and N log lines.
        """
        now = time.time() if now is None else now
        for entry in entries:
            attempts = int(entry.get("attempts", 1))
            entry["next_due"] = now + BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        if entries:
            self.save()

    def expired(self, now: float | None = None) -> list[dict]:
        """Entries past the give-up budget. Removes them."""
        now = time.time() if now is None else now
        gone = [
            e
            for e in self._data.values()
            if now - float(e.get("first_failed", now)) > self.give_up_after
        ]
        for entry in gone:
            self._data.pop(entry["path"], None)
        if gone:
            self.save()
        return gone

    def paths(self) -> set[str]:
        """Queued source paths — baseline seeding must not claim these."""
        return set(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        return True


def retry_path_for(action: str, src: Path, *, to: str | None, mode: str | None) -> Path:
    from folder_watch.support.state import DoneStore

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return RetryQueue.path_for(DoneStore.path_for(action, src, to=to, mode=mode))
