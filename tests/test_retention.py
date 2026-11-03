"""Retention: which presets prune may remove, and when.

A preset is user configuration, and the tool cannot tell a finished project
from a folder that will come back. These pin down the rules that make removing
one safe: the source must be deleted or renamed (never an unmounted volume),
it must have stayed that way for RETENTION_DAYS, nothing may be running it, and
the weekly job only acts on a click.
"""
from __future__ import annotations

import contextlib
import io
import plistlib
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import cli
from folder_watch.presets import load_raw_preset, remove_preset, save_preset
from folder_watch.support import launchd, retention
from folder_watch.support.paths import STATE_DIR

DAY = 86400.0


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.name = f"ret-{self.tmp.name}"
        self.src = self.tmp / "src"
        self.src.mkdir()
        save_preset(
            self.name, {"action": "copy", "src": str(self.src), "to": str(self.tmp)}
        )
        self.addCleanup(remove_preset, self.name)
        self.addCleanup(retention.forget, self.name)

    def since(self, now: float | None = None) -> float | None:
        return dict(retention.review(now)).get(self.name)

    def gone_for(self, days: float) -> None:
        """Delete the source and backdate the first sighting."""
        shutil.rmtree(self.src)
        self.since(time.time() - days * DAY)

    def prune(self, *argv: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run_prune_cli(list(argv))
        return out.getvalue()


class TestReview(RetentionCase):
    def test_a_present_source_is_not_dead(self):
        self.assertIsNone(self.since())

    def test_the_first_sighting_starts_the_clock_and_later_ones_keep_it(self):
        shutil.rmtree(self.src)

        self.assertEqual(self.since(1000.0), 1000.0)
        self.assertEqual(self.since(5000.0), 1000.0)

    def test_a_source_that_comes_back_starts_over(self):
        self.gone_for(40)
        self.src.mkdir()

        self.assertIsNone(self.since())
        shutil.rmtree(self.src)
        self.assertGreater(self.since(), time.time() - 60, "the old clock survived")

    def test_an_unmounted_volume_never_counts(self):
        """Waiting is right for a drive that is not plugged in."""
        save_preset(
            self.name,
            {
                "action": "copy",
                "src": f"/Volumes/NotMounted-{self.tmp.name}/in",
                "to": "/x",
            },
        )

        self.assertIsNone(self.since())

    def test_due_only_after_the_retention_window(self):
        self.assertFalse(retention.is_due(0.0, retention.RETENTION_DAYS * DAY - 60))
        self.assertTrue(retention.is_due(0.0, retention.RETENTION_DAYS * DAY))


class TestPrunePresets(RetentionCase):
    def setUp(self):
        super().setUp()
        self.plist = launchd.service_plist_path(self.name)
        self.logs = [STATE_DIR / f"{self.name}.log", STATE_DIR / f"{self.name}.out.log"]

    def install_leftovers(self):
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text("<plist/>")
        for log in self.logs:
            log.write_text("error reason=src_missing\n")

    def test_a_dry_run_names_it_and_changes_nothing(self):
        self.gone_for(31)
        self.install_leftovers()

        out = self.prune()

        self.assertIn(f"would remove {self.name}", out)
        self.assertIsNotNone(load_raw_preset(self.name))
        self.assertTrue(self.plist.is_file())

    def test_yes_removes_the_preset_its_service_and_its_logs(self):
        self.gone_for(31)
        self.install_leftovers()

        with mock.patch.object(launchd.subprocess, "run"):
            self.prune("--yes")

        self.assertIsNone(load_raw_preset(self.name))
        self.assertFalse(self.plist.exists(), "a LaunchAgent naming a removed preset")
        self.assertEqual([p for p in self.logs if p.exists()], [])
        self.assertIsNone(self.since())

    def test_a_recent_death_is_kept(self):
        self.gone_for(retention.RETENTION_DAYS - 1)

        out = self.prune("--yes")

        self.assertIn("kept until", out)
        self.assertIsNotNone(load_raw_preset(self.name))

    def test_a_preset_with_a_running_watcher_is_kept(self):
        self.gone_for(31)

        with mock.patch.object(cli, "live_watchers", return_value=[4242]):
            out = self.prune("--yes")

        self.assertIn("a watcher is running", out)
        self.assertIsNotNone(load_raw_preset(self.name))


class TestWeeklyAsk(RetentionCase):
    def ask(self, *, clicked: bool):
        with mock.patch.object(cli, "ask", return_value=clicked) as ask:
            self.prune("--ask")
        return ask

    def test_a_click_removes_due_presets(self):
        self.gone_for(31)

        ask = self.ask(clicked=True)

        self.assertIn(self.name, ask.call_args.args[1])
        self.assertIsNone(load_raw_preset(self.name))

    def test_no_click_removes_nothing(self):
        self.gone_for(31)

        self.ask(clicked=False)

        self.assertIsNotNone(load_raw_preset(self.name))

    def test_nothing_due_means_no_notification(self):
        """A weekly job that notifies with nothing to do teaches you to ignore it."""
        self.gone_for(2)

        ask = self.ask(clicked=True)

        ask.assert_not_called()
        self.assertIsNotNone(load_raw_preset(self.name))

    def test_the_schedule_runs_prune_ask_weekly(self):
        with (
            mock.patch.object(
                launchd, "_resolve_python_and_root",
                return_value=(Path("/x/.venv/bin/python"), Path("/x")),
            ),
            mock.patch.object(launchd.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            plist = launchd.install_prune_schedule(program=Path("/x/bin/folder-watch"))
        self.addCleanup(plist.unlink, True)

        job = plistlib.loads(plist.read_bytes())
        self.assertEqual(job["ProgramArguments"][-2:], ["prune", "--ask"])
        self.assertEqual(
            job["StartCalendarInterval"], {"Weekday": 0, "Hour": 10, "Minute": 0}
        )
        self.assertEqual(job["EnvironmentVariables"]["PYTHONPATH"], "/x")


if __name__ == "__main__":
    unittest.main()
