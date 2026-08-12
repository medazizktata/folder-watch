"""Shared filesystem paths and constants."""
from __future__ import annotations

import os
from pathlib import Path


def _dir(env_var: str, default: Path) -> Path:
    """Allow tests (and anyone sandboxing) to redirect state off the real dirs."""
    raw = os.environ.get(env_var)
    return Path(raw).expanduser() if raw else default


CONFIG_DIR = _dir("FOLDER_WATCH_CONFIG_DIR", Path.home() / ".config" / "folder-watch")
PRESETS_PATH = CONFIG_DIR / "presets.json"
LEGACY_PRESETS = Path.home() / ".config" / "watch-copy" / "presets.json"
STATE_DIR = _dir(
    "FOLDER_WATCH_STATE_DIR", Path.home() / ".local" / "state" / "folder-watch"
)
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.folder-watch"
PID_DIR = STATE_DIR / "pids"


class DestUnavailable(OSError):
    """Destination's parent is missing — likely an unmounted drive."""


def ensure_dest_dir(dest: Path, *, create_parents: bool = False) -> None:
    """Make `dest` usable, refusing to conjure a missing mount out of thin air.

    mkdir(parents=True) on an unmounted Drive path happily recreates the whole
    tree as ordinary local directories, so files land on the boot disk and get
    recorded as done. Requiring the parent to already exist tells "output
    subfolder not created yet" apart from "the volume isn't there".
    """
    dest = dest.expanduser()
    if dest.is_dir():
        return
    if dest.exists():
        # Otherwise mkdir surfaces a bare [Errno 17] File exists, which reads
        # like a race rather than "your --to points at a file".
        raise DestUnavailable(f"destination exists but is not a directory: {dest}")
    if create_parents:
        dest.mkdir(parents=True, exist_ok=True)
        return
    parent = dest.parent
    if not parent.is_dir():
        raise DestUnavailable(
            f"destination parent does not exist: {parent} "
            "(unmounted drive? pass --create-dest to build the full path)"
        )
    dest.mkdir(exist_ok=True)
