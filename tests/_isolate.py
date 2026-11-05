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

# The unit suite must not post notifications, drive Finder or talk to the real
# launchd: a run of it once sent twelve real alerter notifications (an
# UnzipAction notifies when done unless told not to). Stubs for each go first
# on PATH, which every watcher a test spawns inherits; `launchctl print`
# answers as for an unknown job. The e2e suite drives the real ones on purpose
# and opts out.
# A prefix set in the shell would rename every label the unit tests expect;
# the e2e suite sets its own on purpose.
if not os.environ.get("FOLDER_WATCH_E2E"):
    os.environ.pop("FOLDER_WATCH_LABEL_PREFIX", None)

STUBS = SCRATCH / "stub-bin"
if not os.environ.get("FOLDER_WATCH_E2E"):
    STUBS.mkdir(parents=True, exist_ok=True)
    for _tool in ("alerter", "terminal-notifier", "osascript", "open", "launchctl"):
        _stub = STUBS / _tool
        _stub.write_text(
            "#!/bin/sh\n"
            f'echo "{_tool} $*" >> "{STUBS}/calls.log"\n'
            + ('[ "$1" = print ] && exit 113\n' if _tool == "launchctl" else "")
            + "exit 0\n"
        )
        _stub.chmod(0o755)
    if not os.environ.get("PATH", "").startswith(f"{STUBS}:"):
        os.environ["PATH"] = f"{STUBS}:{os.environ.get('PATH', '')}"
