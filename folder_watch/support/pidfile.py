"""Watcher pidfiles for status."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from folder_watch.support.log import _jsonable
from folder_watch.support.paths import PID_DIR


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or "default"


def pidfile_path(name: str, pid: int | None = None) -> Path:
    """One file per process, not per watch name.

    Keying only on the name meant a second watcher on the same folder
    overwrote the first's pidfile: `status` then showed one row carrying the
    second's PID, and once that exited it reported the survivor as STOPPED
    with a stale pidfile — while it was still running and copying.
    """
    PID_DIR.mkdir(parents=True, exist_ok=True)
    return PID_DIR / f"{safe_name(name)}.{pid or os.getpid()}.json"


def write_pidfile(name: str, meta: dict[str, Any]) -> Path:
    path = pidfile_path(name)
    payload = {
        "pid": os.getpid(),
        "name": name,
        "started": time.time(),
        **{k: _jsonable(v) for k, v in meta.items()},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def clear_pidfile(name: str) -> None:
    try:
        pidfile_path(name).unlink(missing_ok=True)
    except OSError:
        pass


def live_watchers(name: str) -> list[int]:
    """PIDs of other running watchers already using this name."""
    out: list[int] = []
    if not PID_DIR.is_dir():
        return out
    me = os.getpid()
    for path in sorted(PID_DIR.glob(f"{safe_name(name)}.*.json")):
        data = read_pidfile(path)
        if data is None:
            continue
        pid = int(data.get("pid") or 0)
        if pid and pid != me and _pid_alive(pid):
            out.append(pid)
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pidfile(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def prune_pidfiles(*, dry_run: bool = False) -> list[Path]:
    """Remove pidfiles whose process is gone. Returns what was (or would be) removed."""
    removed: list[Path] = []
    if not PID_DIR.is_dir():
        return removed
    for path in sorted(PID_DIR.glob("*.json")):
        data = read_pidfile(path)
        pid = int((data or {}).get("pid") or 0)
        if data is not None and _pid_alive(pid):
            continue
        removed.append(path)
        if not dry_run:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return removed


def collect_watch_status(
    *,
    preset: str | None = None,
    log_tail: int = 12,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    PID_DIR.mkdir(parents=True, exist_ok=True)
    # Filter on the recorded name, not the filename — pidfiles are per-process
    # now, so one name can legitimately have several.
    paths = sorted(PID_DIR.glob("*.json"))
    for path in paths:
        data = read_pidfile(path)
        if data is None:
            continue
        if preset and (data.get("name") or path.stem) != preset:
            continue
        pid = int(data.get("pid") or 0)
        alive = _pid_alive(pid)
        log_path = data.get("log")
        tail: list[str] = []
        if log_path and Path(str(log_path)).is_file():
            try:
                lines = Path(str(log_path)).read_text(errors="replace").splitlines()
                tail = lines[-log_tail:]
            except OSError:
                pass
        rows.append(
            {
                "name": data.get("name") or path.stem,
                "pid": pid,
                "alive": alive,
                "pidfile": str(path),
                "src": data.get("src"),
                "action": data.get("action"),
                "to": data.get("to"),
                "preset": data.get("preset"),
                "log": log_path,
                "started": data.get("started"),
                "tail": tail,
                "note": None if alive else "stale pidfile (process not running)",
            }
        )
    return rows
