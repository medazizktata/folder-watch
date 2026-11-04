"""`folder-watch doctor`: each check reports the problem it exists for.

launchctl and the disk are faked here. The launchd probe itself needs a real
launchd and was checked live: Homebrew's Python could read ~/Desktop, Apple's
/usr/bin/python3 got "Operation not permitted", and the same interpreter read
~/Projects fine.
"""
from __future__ import annotations

import json
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import doctor
from folder_watch.support.paths import LAUNCH_AGENTS, PRESETS_PATH


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = PRESETS_PATH.read_text() if PRESETS_PATH.is_file() else None
        self.addCleanup(
            lambda: PRESETS_PATH.write_text(saved)
            if saved is not None
            else PRESETS_PATH.unlink(missing_ok=True)
        )
        self.plists: list[Path] = []
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in self.plists])

    def presets(self, **presets):
        PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRESETS_PATH.write_text(json.dumps(presets))

    def service(self, name: str) -> None:
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        path = LAUNCH_AGENTS / f"com.folder-watch.{name}.plist"
        path.write_bytes(plistlib.dumps({"Label": f"com.folder-watch.{name}"}))
        self.plists.append(path)

    def levels(self, findings, needle: str) -> list[str]:
        return [level for level, message in findings if needle in message]


class TestServices(DoctorCase):
    def run_check(self, prints: dict[str, dict[str, str]]):
        with (
            mock.patch.object(
                doctor, "_launchctl_print", side_effect=lambda lab: prints.get(lab, {})
            ),
            mock.patch.object(doctor, "service_status", return_value={}),
        ):
            return doctor.check_services()

    def test_a_crashing_service_is_an_error(self):
        """A crash before folder_watch loads leaves no log; the exit code is
        the only trace, and launchd restarts it regardless."""
        self.presets(job={"action": "copy", "src": "/a", "to": "/b"})
        self.service("job")

        found = self.run_check(
            {"com.folder-watch.job": {"last exit code": "1", "runs": "40"}}
        )

        self.assertIn("error", self.levels(found, "last exit 1 after 40"))

    def test_a_healthy_service_is_ok(self):
        self.presets(job={"action": "copy", "src": "/a", "to": "/b"})
        self.service("job")

        found = self.run_check(
            {"com.folder-watch.job": {"state": "running", "last exit code": "0"}}
        )

        self.assertEqual(self.levels(found, "service job"), ["ok"])

    def test_not_loaded_and_orphaned_services_are_errors(self):
        self.presets()
        self.service("unloaded")
        self.service("orphan")

        found = self.run_check(
            {"com.folder-watch.orphan": {"state": "running", "last exit code": "0"}}
        )

        self.assertIn("error", self.levels(found, "unloaded: plist installed but not"))
        self.assertIn("error", self.levels(found, "orphan: its preset no longer exists"))


class TestDestinations(DoctorCase):
    def test_missing_unwritable_and_full_destinations(self):
        ok, ro, full = (self.tmp / n for n in ("ok", "ro", "full"))
        for d in (ok, ro, full):
            d.mkdir()
        ro.chmod(0o555)
        self.addCleanup(ro.chmod, 0o755)
        self.presets(
            a={"action": "copy", "src": "/s", "to": str(ok)},
            b={"action": "copy", "src": "/s", "to": str(ro)},
            c={"action": "copy", "src": "/s", "to": str(full)},
            d={"action": "copy", "src": "/s", "to": str(self.tmp / "nope")},
        )
        real = shutil.disk_usage

        def usage(path):
            u = real(path)
            return u._replace(free=10) if Path(path) == full else u

        with mock.patch.object(doctor.shutil, "disk_usage", side_effect=usage):
            found = doctor.check_destinations()

        self.assertEqual(self.levels(found, "a:"), ["ok"])
        self.assertEqual(self.levels(found, "b:"), ["error"])
        self.assertEqual(self.levels(found, "c:"), ["warn"])
        self.assertEqual(self.levels(found, "d:"), ["warn"])


class TestNotifiersAndProtection(unittest.TestCase):
    def test_a_missing_notifier_is_a_warning(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            found = doctor.check_notifiers()

        self.assertEqual({level for level, _ in found}, {"warn"})

    def test_only_desktop_documents_and_downloads_count_as_protected(self):
        home = Path.home()
        self.assertEqual(doctor.protected_category(home / "Desktop/x/y"), "Desktop")
        self.assertEqual(doctor.protected_category(home / "Downloads"), "Downloads")
        self.assertIsNone(doctor.protected_category(home / "Projects/x"))
        self.assertIsNone(doctor.protected_category(Path("/Volumes/Drive/Desktop")))


if __name__ == "__main__":
    unittest.main()
