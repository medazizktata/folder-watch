"""FS helpers: lsof writers, ext matching."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from folder_watch.support.log import emit
from folder_watch.support.partials import is_partial_filename

_LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "/bin/lsof")
_warned_no_lsof = False


def lsof_binary() -> str | None:
    """Locate lsof without relying on PATH.

    macOS keeps it in /usr/sbin, which launchd's default PATH does not
    include — so a watcher started as a LaunchAgent could not find it at all.
    """
    found = shutil.which("lsof")
    if found:
        return found
    for candidate in _LSOF_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def is_open(path: Path) -> int:
    """Count writers on path (AME encode). Ignore read-only opens (preview)."""
    global _warned_no_lsof
    binary = lsof_binary()
    if binary is None:
        # Degrade to size-stability rather than deadlock. Reporting "open"
        # here would mean nothing ever settles and the watcher silently
        # processes nothing at all.
        if not _warned_no_lsof:
            _warned_no_lsof = True
            emit("warn", reason="lsof_unavailable", note="settling on size alone")
        return 0
    try:
        r = subprocess.run(
            [binary, str(path)],
            capture_output=True,
            text=True,
            check=False,
            # This runs on the single-threaded main loop. lsof walks every FD
            # on the machine and can wedge on an unresponsive mount; without a
            # bound it would stall the whole watcher indefinitely.
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        # lsof exists but wedged, probably on an unresponsive mount. Unknown
        # is not "closed": wait rather than act on a possibly-partial file.
        return 1
    except OSError:
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

