"""Point folder-watch's state and config at a scratch dir, not the real ones.

Import this BEFORE importing folder_watch — support/paths.py reads these at
import time. Without it the suites write done-state and pidfiles into
~/.local/state/folder-watch/, which is how ~60 orphaned done-*.json files and a
stale pidfile for a long-gone temp directory ended up there.

setdefault, and a fixed path, so every test module and every subprocess they
spawn agree on the same scratch location regardless of import order.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.gettempdir()) / "folder-watch-tests"

os.environ.setdefault("FOLDER_WATCH_STATE_DIR", str(SCRATCH / "state"))
os.environ.setdefault("FOLDER_WATCH_CONFIG_DIR", str(SCRATCH / "config"))
# Keep the real ~/.config/folder-watch/.env from leaking into test runs.
os.environ.setdefault("FOLDER_WATCH_ENV", str(SCRATCH / "config" / ".env"))
# Nothing in the suite may write a plist into the real ~/Library/LaunchAgents,
# where a stray file is a service macOS will actually try to run at login.
os.environ.setdefault("FOLDER_WATCH_LAUNCH_AGENTS", str(SCRATCH / "launch-agents"))

STATE_DIR = Path(os.environ["FOLDER_WATCH_STATE_DIR"])
CONFIG_DIR = Path(os.environ["FOLDER_WATCH_CONFIG_DIR"])
LAUNCH_AGENTS = Path(os.environ["FOLDER_WATCH_LAUNCH_AGENTS"])
for _d in (STATE_DIR, CONFIG_DIR, LAUNCH_AGENTS):
    _d.mkdir(parents=True, exist_ok=True)
