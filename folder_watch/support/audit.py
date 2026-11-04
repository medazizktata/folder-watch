"""The audit trail: one JSON line per thing the tool did, and who asked.

Logs rotate away and the done-store forgets on purpose; neither can answer
"what happened to this preset last month, and was it me". This file is
append-only, rotation never touches it, and prune keeps a year of it.

Each line carries the time, a kind (copied, preset_saved, service_paused,
...), the preset when there is one, the details, and the trigger: cli,
service, weekly, dashboard, notification or automatic. The trigger comes from
the code path that asked (`triggered_by`), else from how the process was
started.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from folder_watch.support.paths import LABEL_PREFIX, STATE_DIR

AUDIT = STATE_DIR / "audit.jsonl"
KEEP_DAYS = 365

_trigger: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trigger", default=None
)
_preset: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "preset", default=None
)


def default_trigger() -> str:
    job = os.environ.get("XPC_SERVICE_NAME", "")
    if job == f"{LABEL_PREFIX}._weekly-prune":
        return "weekly"
    if job.startswith(LABEL_PREFIX + "."):
        return "service"
    return "cli"


@contextlib.contextmanager
def triggered_by(trigger: str) -> Iterator[None]:
    """Attribute everything recorded inside to `trigger`."""
    token = _trigger.set(trigger)
    try:
        yield
    finally:
        _trigger.reset(token)


def set_trigger(trigger: str) -> None:
    """For a thread that only ever acts for one trigger, such as a dashboard
    request; elsewhere use triggered_by()."""
    _trigger.set(trigger)


def set_preset(name: str | None) -> None:
    """The preset a running watcher works for, so file outcomes say which."""
    _preset.set(name)


def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


@contextlib.contextmanager
def _locked(operation: int) -> Iterator[None]:
    """Appends share the lock and trim takes it alone, so a line appended
    while trim rewrites the trail cannot land in the copy being replaced."""
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT.with_name(AUDIT.name + ".lock"), "a") as handle:
        fcntl.flock(handle, operation)
        yield


def record(kind: str, *, preset: str | None = None, **fields: Any) -> None:
    """Append one line. Never raises: an audit trail that could stop a copy
    would cost more than the line it failed to write."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "preset": preset if preset is not None else _preset.get(),
        "trigger": _trigger.get() or default_trigger(),
        **{k: _jsonable(v) for k, v in fields.items()},
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        # O_APPEND: lines from several processes land whole, one after another.
        with _locked(fcntl.LOCK_SH):
            fd = os.open(AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                # A trail written before this was 0644; what it records is yours.
                os.fchmod(fd, 0o600)
                os.write(fd, line.encode())
            finally:
                os.close(fd)
    except OSError:
        pass


def read(
    *,
    since: datetime | None = None,
    preset: str | None = None,
    kinds: set[str] | None = None,
) -> list[dict[str, Any]]:
    entries = []
    try:
        lines = AUDIT.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
            when = datetime.fromisoformat(entry["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if since is not None and when < since:
            continue
        if preset is not None and entry.get("preset") != preset:
            continue
        if kinds and entry.get("kind") not in kinds:
            continue
        entries.append(entry)
    return entries


def parse_since(text: str, now: datetime | None = None) -> datetime:
    """`7d`, `12h`, `30m`, or an ISO date or date-time."""
    now = now or datetime.now()
    units = {"d": "days", "h": "hours", "m": "minutes"}
    if text[-1:] in units and text[:-1].isdigit():
        return now - timedelta(**{units[text[-1]]: int(text[:-1])})
    when = datetime.fromisoformat(text)
    # Entries carry local time without an offset; compare like with like.
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def trim(keep_days: int = KEEP_DAYS, now: float | None = None, *, dry: bool) -> int:
    """Drop entries older than `keep_days`; returns how many (would) go.
    Rewritten through a temp file, so a crash leaves the old trail whole."""
    cutoff = datetime.fromtimestamp(now if now is not None else time.time()) - timedelta(
        days=keep_days
    )
    with _locked(fcntl.LOCK_SH if dry else fcntl.LOCK_EX):
        try:
            lines = AUDIT.read_text("utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            return 0
        kept = []
        for line in lines:
            try:
                if datetime.fromisoformat(json.loads(line)["ts"]) < cutoff:
                    continue
            except (ValueError, KeyError, TypeError):
                pass  # keep what cannot be dated rather than lose it
            kept.append(line)
        gone = len(lines) - len(kept)
        if gone and not dry:
            tmp = AUDIT.with_suffix(".jsonl.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)  # a temp file left by an older run keeps its mode
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(kept))
            os.replace(tmp, AUDIT)
    return gone


def describe(entry: dict[str, Any]) -> str:
    """The details of one entry as a short line: what changed for a saved
    preset, key=value for anything else."""
    if entry.get("kind") == "preset_saved":
        changes = entry.get("changes") or {}
        if entry.get("created"):
            return "created"
        text = ", ".join(f"{k}: {b} -> {a}" for k, (b, a) in changes.items())
        return text or "no change"
    skip = {"ts", "kind", "preset", "trigger"}
    return " ".join(f"{k}={v}" for k, v in entry.items() if k not in skip)
