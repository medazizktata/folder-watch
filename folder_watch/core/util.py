"""FS helpers: lsof writers, ext matching."""
from __future__ import annotations

import subprocess
from pathlib import Path

from folder_watch.support.partials import is_partial_filename


def is_open(path: Path) -> int:
    """Count writers on path (AME encode). Ignore read-only opens (preview)."""
    try:
        r = subprocess.run(
            ["lsof", str(path)],
            capture_output=True,
            text=True,
            check=False,
            # This runs on the single-threaded main loop. lsof walks every FD
            # on the machine and can wedge on an unresponsive mount; without a
            # bound it would stall the whole watcher indefinitely.
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Unknown, not "closed" — treat as still open so we wait rather than
        # acting on a file that may still be being written.
        return 1
    n = 0
    for ln in r.stdout.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 4:
            continue
        fd = parts[3]
        # lsof FD mode suffix: u=read/write, w=write, r=read-only
        if fd and fd[-1] in ("u", "w"):
            n += 1
    return n


def match_ext(name: str, exts: list[str] | None) -> bool:
    if not exts:
        return True
    lower = name.lower()
    return any(lower.endswith("." + e.lower().lstrip(".")) for e in exts)


def should_watch_name(name: str, exts: list[str] | None) -> bool:
    """Extension match and not a partial/scratch filename."""
    if is_partial_filename(name):
        return False
    return match_ext(name, exts)


def parse_exts(raw: str) -> list[str] | None:
    if raw.strip() == "*":
        return None
    return [e.strip() for e in raw.split(",") if e.strip()]

