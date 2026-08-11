"""Shared filesystem paths and constants."""
from __future__ import annotations

from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "folder-watch"
PRESETS_PATH = CONFIG_DIR / "presets.json"
LEGACY_PRESETS = Path.home() / ".config" / "watch-copy" / "presets.json"
STATE_DIR = Path.home() / ".local" / "state" / "folder-watch"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.folder-watch"
PID_DIR = STATE_DIR / "pids"
