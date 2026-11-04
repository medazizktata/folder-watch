"""`status --all`, and pausing and resuming a service.

launchctl is faked here; tests/e2e pauses and resumes a real job.
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

from folder_watch import dashboard, doctor
from folder_watch.presets import remove_preset, save_preset
from folder_watch.support import launchd
from folder_watch.support.paths import STATE_DIR


class TestLastResult(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def last(self, *lines: str) -> str:
        log = self.tmp / "p.log"
        log.write_text("\n".join(lines) + "\n")
        return dashboard.last_result(log)

    def test_the_newest_result_wins_in_the_plain_format(self):
        self.assertEqual(
            self.last(
                "09:00:00 INFO copied name=old.mp4 size=1",
                "09:05:00 INFO copied name=\"new clip.mp4\" size=2",
                "09:06:00 INFO progress name=next.mp4 size=3",
            ),
            "copied new clip.mp4 at 09:05:00",
        )

    def test_dated_lines_show_their_date(self):
        self.assertEqual(
            self.last("2026-11-04 09:05:00 INFO copied name=a.mp4 size=1"),
            "copied a.mp4 at 2026-11-04 09:05:00",
        )

    def test_a_brace_in_a_file_name_is_not_json(self):
        self.assertEqual(
            self.last("09:00:00 INFO copied name=a{b}.mp4 size=1"),
            "copied a{b}.mp4 at 09:00:00",
        )

    def test_json_lines_are_read_too(self):
        self.assertEqual(
            self.last(json.dumps({"event": "extracted", "name": "pack.zip"})),
            "extracted pack.zip",
        )

    def test_no_result_and_no_log_are_dashes(self):
        self.assertEqual(self.last("09:00:00 INFO start src=/a"), "-")
        self.assertEqual(dashboard.last_result(self.tmp / "missing.log"), "-")


class TestServiceColumn(unittest.TestCase):
    def setUp(self):
        self.name = f"dash-{id(self)}"
        self.plist = launchd.service_plist_path(self.name)
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        label = launchd.service_label(self.name)
        self.plist.write_bytes(plistlib.dumps({"Label": label}))
        self.addCleanup(self.plist.unlink, True)

    def column(self, *, paused=False, info=None):
        with (
            mock.patch.object(dashboard, "is_paused", return_value=paused),
            mock.patch.object(dashboard, "_launchctl_print", return_value=info or {}),
        ):
            return dashboard.service_column(self.name)

    def test_each_state(self):
        self.assertEqual(self.column(paused=True), ("paused", "-"))
        self.assertEqual(self.column(), ("not loaded", "-"))
        self.assertEqual(
            self.column(info={"state": "running", "last exit code": "(never exited)"}),
            ("running", "-"),
        )
        self.assertEqual(
            self.column(info={"state": "not running", "last exit code": "1"}),
            ("not running", "1"),
        )

    def test_paths_are_shown_from_home(self):
        home = str(Path.home())
        self.assertEqual(
            dashboard.home_relative(f"missing: nearest is {home}/Desktop"),
            "missing: nearest is ~/Desktop",
        )
        self.assertEqual(dashboard.home_relative(home), "~")

    def test_no_service_installed(self):
        self.plist.unlink()
        self.assertEqual(dashboard.service_column(self.name), ("-", "-"))


class TestRows(unittest.TestCase):
    def test_a_row_per_preset_with_health_and_log_size(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        live, dead = f"live-{id(self)}", f"dead-{id(self)}"
        save_preset(live, {"action": "copy", "src": str(tmp), "to": str(tmp)})
        save_preset(dead, {"action": "copy", "src": str(tmp / "gone"), "to": str(tmp)})
        self.addCleanup(remove_preset, live)
        self.addCleanup(remove_preset, dead)
        log = STATE_DIR / f"{live}.log"
        log.write_text("10:00:00 INFO copied name=a.mp4 size=1\n" + "x" * 2 * 1024**2)
        self.addCleanup(log.unlink, True)

        by_name = {r["preset"]: r for r in dashboard.rows()}

        self.assertEqual(by_name[live]["health"], "ok")
        self.assertEqual(by_name[live]["log"], "2.0MB")
        self.assertEqual(
            by_name[live]["last result"], "copied a.mp4 at 10:00:00",
            "the last result sits 2 MB back in the log",
        )
        self.assertTrue(by_name[dead]["health"].startswith("error: source missing"))
        self.assertIn(live, dashboard.render(dashboard.rows()))


class TestPauseResume(unittest.TestCase):
    def setUp(self):
        self.name = f"pause-{id(self)}"
        self.plist = launchd.service_plist_path(self.name)
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        label = launchd.service_label(self.name)
        self.plist.write_bytes(plistlib.dumps({"Label": label}))
        self.addCleanup(self.plist.unlink, True)
        self.target = f"gui/{launchd.os.getuid()}/{launchd.service_label(self.name)}"

    def calls(self, fn, returncode=0):
        with mock.patch.object(launchd, "_launchctl") as ctl:
            ctl.return_value.returncode = returncode
            ctl.return_value.stderr = "boom"
            fn(preset=self.name)
        return [c.args for c in ctl.call_args_list]

    def test_pause_disables_then_stops(self):
        """Disabled first, so a restart cannot slip in between."""
        self.assertEqual(
            self.calls(launchd.pause_service),
            [("disable", self.target), ("bootout", self.target)],
        )

    def test_resume_enables_then_loads(self):
        calls = self.calls(launchd.resume_service)
        self.assertEqual(calls[0], ("enable", self.target))
        self.assertEqual(calls[1][0], "bootstrap")

    def test_resume_of_a_running_service_is_fine_and_other_errors_raise(self):
        self.calls(launchd.resume_service, returncode=37)
        with self.assertRaises(RuntimeError):
            self.calls(launchd.resume_service, returncode=5)

    def test_without_a_service_both_refuse(self):
        self.plist.unlink()
        for fn in (launchd.pause_service, launchd.resume_service):
            with self.assertRaises(RuntimeError):
                self.calls(fn)

    def test_uninstall_clears_a_pause(self):
        """A pause outlives the plist; a later install would load disabled."""
        with mock.patch.object(launchd.subprocess, "run"):
            calls = self.calls(launchd.uninstall_service)
        self.assertIn(("enable", self.target), calls)

    def test_is_paused_reads_both_formats(self):
        label = launchd.service_label(self.name)
        for listing in (f'\t"{label}" => disabled', f'\t"{label}" => true'):
            with mock.patch.object(launchd, "_launchctl") as ctl:
                ctl.return_value.stdout = listing
                self.assertTrue(launchd.is_paused(self.name), listing)
        with mock.patch.object(launchd, "_launchctl") as ctl:
            ctl.return_value.stdout = f'\t"{label}" => enabled'
            self.assertFalse(launchd.is_paused(self.name))

    def test_doctor_reports_a_paused_service_as_paused(self):
        save_preset(self.name, {"action": "copy", "src": "/a", "to": "/b"})
        self.addCleanup(remove_preset, self.name)
        with (
            mock.patch.object(doctor, "_launchctl_print", return_value={}),
            mock.patch.object(doctor, "is_paused", return_value=True),
        ):
            found = doctor.check_services()
        self.assertIn(("ok", f"service {self.name}: paused"), found)


if __name__ == "__main__":
    unittest.main()
