"""Fixtures for end-to-end tests: real watcher processes, real FSEvents, and
real launchd jobs, each in a throwaway tree.

Not part of `make check`: like the smoke suite these depend on timing outside
the process, and a slow or flaky commit gate gets bypassed. `make e2e` runs
them, and `make all`, `make feature` and CI include them.

Every wait polls for a condition under a deadline. A sleep on its own is a
guess about someone else's machine, and test_harness enforces that rule here
too.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

DEADLINE = 30.0
POLL = 0.05


def wait_until(condition: Callable[[], Any], timeout: float = DEADLINE) -> Any:
    """Poll `condition` until it returns something truthy, or None at the
    deadline. Returns the truthy value so callers can use what it found."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(POLL)
    return None


class IsolatedCase(unittest.TestCase):
    """A throwaway tree under the temp dir, asserted rather than assumed: the
    cost of being wrong is somebody's real folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fw-e2e-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()
        scratch = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(str(self.tmp).startswith(str(scratch)), self.tmp)
        for var in ("FOLDER_WATCH_STATE_DIR", "FOLDER_WATCH_CONFIG_DIR",
                    "FOLDER_WATCH_LAUNCH_AGENTS"):
            self.assertIn("folder-watch-tests", os.environ.get(var, ""), var)

    def drop(self, name: str, size: int = 2_000_000) -> Path:
        """A finished file, as an app that saves atomically would leave it:
        written beside the folder, then renamed in."""
        staged = self.tmp / f".staging-{name}"
        staged.write_bytes(os.urandom(size))
        final = self.src / name
        staged.rename(final)
        return final

    def drop_zip(self, name: str, members: dict[str, bytes]) -> Path:
        staged = self.tmp / f".staging-{name}"
        with zipfile.ZipFile(staged, "w") as z:
            for member, data in members.items():
                z.writestr(member, data)
        final = self.src / name
        staged.rename(final)
        return final


class WatcherCase(IsolatedCase):
    """A real `folder-watch` process, its events read back as JSON."""

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "watch.log"
        self.proc: subprocess.Popen | None = None
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def start(self, *argv: str) -> subprocess.Popen:
        """Start a watcher. JSON events go to self.log; --no-notify keeps the
        run off the desktop. Waits for its start event, so a test never races
        the watcher's own startup."""
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "folder_watch", *argv,
             "--no-notify", "--json", "--log", str(self.log)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        self.assertTrue(
            wait_until(lambda: self.events("start") or self.proc.poll() is not None),
            "the watcher neither started nor exited",
        )
        return self.proc

    def events(self, kind: str | None = None, **match: Any) -> list[dict]:
        """Every logged event, optionally of one kind and with matching fields."""
        if not self.log.is_file():
            return []
        found = []
        for line in self.log.read_text(errors="replace").splitlines():
            start = line.find("{")
            if start < 0:
                continue
            try:
                event = json.loads(line[start:])
            except json.JSONDecodeError:
                continue
            if kind is not None and event.get("event") != kind:
                continue
            if all(str(event.get(k)) == str(v) for k, v in match.items()):
                found.append(event)
        return found

    def wait_for(self, kind: str, timeout: float = DEADLINE, **match: Any) -> dict:
        found = wait_until(lambda: self.events(kind, **match), timeout)
        if not found:
            seen = [e.get("event") for e in self.events()]
            self.fail(f"no {kind} {match or ''} within {timeout}s; events: {seen}")
        return found[-1]

    def exit_code(self, timeout: float = DEADLINE) -> int | None:
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> int | None:
        assert self.proc is not None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        return self.exit_code()


def launchd_available() -> bool:
    """A GUI login session to load user agents into. CI runners have none."""
    r = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}"], capture_output=True, check=False
    )
    return r.returncode == 0


@unittest.skipUnless(launchd_available(), "no gui launchd domain (e.g. a CI runner)")
class LaunchdCase(IsolatedCase):
    """Real launchd jobs under unique labels, pointed at the scratch config
    and state, and always unloaded again, whatever the test did."""

    def setUp(self):
        super().setUp()
        self.domain = f"gui/{os.getuid()}"
        self.labels: list[str] = []
        self.addCleanup(self._unload_all)

    def _unload_all(self) -> None:
        for label in self.labels:
            subprocess.run(["launchctl", "bootout", f"{self.domain}/{label}"],
                           capture_output=True, check=False)

    def load(self, plist: dict) -> str:
        """Load a job. The scratch dirs ride along in its environment, since a
        launchd job inherits nothing from this process."""
        label = plist["Label"]
        env = dict(plist.get("EnvironmentVariables", {}))
        for var in ("FOLDER_WATCH_STATE_DIR", "FOLDER_WATCH_CONFIG_DIR",
                    "FOLDER_WATCH_LAUNCH_AGENTS"):
            env[var] = os.environ[var]
        plist = {**plist, "EnvironmentVariables": env}
        path = self.tmp / f"{label}.plist"
        path.write_bytes(plistlib.dumps(plist))
        self.labels.append(label)
        r = subprocess.run(["launchctl", "bootstrap", self.domain, str(path)],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return label

    def unique_label(self, what: str) -> str:
        return f"com.folder-watch.e2e-{what}-{os.getpid()}-{time.monotonic_ns()}"

    def job(self, label: str) -> dict[str, str]:
        """launchctl's view of a job: state, runs, last exit code."""
        r = subprocess.run(["launchctl", "print", f"{self.domain}/{label}"],
                           capture_output=True, text=True, check=False)
        info: dict[str, str] = {}
        for line in r.stdout.splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key in ("state", "runs", "last exit code") and key not in info:
                info[key] = value
        return info

    def runs(self, label: str) -> int:
        return int(self.job(label).get("runs", "0"))
