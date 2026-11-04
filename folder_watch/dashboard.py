"""`folder-watch status --all`: every preset on one screen.

Each column already existed somewhere (--list-presets for health, launchctl
for the service, the log for the last result); having to run three commands
and read a log to answer "is everything fine?" meant nobody asked.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from folder_watch.doctor import _launchctl_print
from folder_watch.presets import load_presets, preset_status
from folder_watch.support.launchd import is_paused, service_label, service_plist_path
from folder_watch.support.notify import human_size
from folder_watch.support.paths import STATE_DIR

RESULTS = ("copied", "extracted", "ran")
# Lines written before dates were added start with the time alone.
_PLAIN = re.compile(r"^(?:((?:\d{4}-\d\d-\d\d )?\d\d:\d\d:\d\d) \w+ )?(\w+) (.*)$")
_NAME = re.compile(r'name=("(?:[^"\\]|\\.)*"|\S+)')


def last_result(log: Path) -> str:
    """The newest copied/extracted/ran in a log, plain or --json format.

    The whole log, not a tail: progress lines from one busy run can push the
    last result arbitrarily far back, and rotation caps the file at 10 MB."""
    try:
        lines = log.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return "-"
    for line in reversed(lines):
        # --json lines are pure JSON; a plain line can still contain a brace
        # in a file name, and must not be mistaken for one.
        if line.startswith("{"):
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict) and event.get("event") in RESULTS:
                return f"{event['event']} {event.get('name', '?')}"
            continue
        m = _PLAIN.match(line)
        if m and m.group(2) in RESULTS:
            name = _NAME.search(m.group(3))
            when = f" at {m.group(1)}" if m.group(1) else ""
            return f"{m.group(2)} {name.group(1).strip(chr(34)) if name else '?'}{when}"
    return "-"


def log_bytes(name: str) -> int:
    """Default log, its rotated copies and launchd's .out log."""
    paths = [*STATE_DIR.glob(f"{name}.log*"), STATE_DIR / f"{name}.out.log"]
    return sum(p.stat().st_size for p in set(paths) if p.is_file())


def service_column(name: str) -> tuple[str, str]:
    """(service state, last exit code) for a preset's LaunchAgent."""
    if not service_plist_path(name).is_file():
        return "-", "-"
    if is_paused(name):
        return "paused", "-"
    info = _launchctl_print(service_label(name))
    if not info:
        return "not loaded", "-"
    state = "running" if info.get("state") == "running" else info.get("state", "loaded")
    code = info.get("last exit code", "-")
    # launchd's wording for a job that has not exited since it was loaded.
    return state, "-" if code == "(never exited)" else code


def home_relative(text: str) -> str:
    home = str(Path.home())
    return text.replace(home + "/", "~/").replace(home, "~")


def entries() -> list[dict[str, Any]]:
    """Every preset with raw fields: what the web dashboard renders."""
    out = []
    for name, cfg in sorted(load_presets().items()):
        level, note = preset_status(cfg)
        service, code = service_column(name)
        out.append({
            "preset": name,
            "level": level,
            "note": home_relative(note),
            "action": cfg.get("action", "copy"),
            "src": home_relative(str(cfg.get("src", ""))),
            "to": home_relative(str(cfg.get("to") or "")),
            "service": service,
            "exit": code,
            "last": last_result(STATE_DIR / f"{name}.log"),
            "log_bytes": log_bytes(name),
        })
    return out


def rows() -> list[dict[str, str]]:
    """The same, formatted for the terminal table."""
    return [
        {
            "preset": e["preset"],
            "health": "ok" if e["level"] == "ok" else f"{e['level']}: {e['note']}",
            "service": e["service"],
            "exit": e["exit"],
            "last result": e["last"],
            "log": human_size(e["log_bytes"]) if e["log_bytes"] else "-",
        }
        for e in entries()
    ]


def render(table: list[dict[str, str]]) -> str:
    if not table:
        return "no presets"
    cols = list(table[0])
    width = {c: max(len(c), *(len(r[c]) for r in table)) for c in cols}
    lines = ["  ".join(c.upper().ljust(width[c]) for c in cols)]
    lines += ["  ".join(r[c].ljust(width[c]) for c in cols) for r in table]
    return "\n".join(line.rstrip() for line in lines)
