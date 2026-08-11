"""Watcher pidfiles for status."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from folder_watch.support.log import _jsonable
from folder_watch.support.paths import PID_DIR


def pidfile_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or "default"
    PID_DIR.mkdir(parents=True, exist_ok=True)
    return PID_DIR / f"{safe}.json"


def write_pidfile(name: str, meta: dict[str, Any]) -> Path:
    path = pidfile_path(name)
    payload = {
        "pid": os.getpid(),
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
    paths = [pidfile_path(preset)] if preset else sorted(PID_DIR.glob("*.json"))
    for path in paths:
        data = read_pidfile(path)
        if data is None:
            if preset:
                rows.append(
                    {
                        "name": preset,
                        "pidfile": str(path),
                        "alive": False,
                        "note": "no pidfile",
                    }
                )
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
