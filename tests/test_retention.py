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
import os
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

    def test_a_source_last_seen_on_an_ejected_volume_never_counts(self):
        """Mounted outside /Volumes, an ejected volume looks deleted; the mount
        the source was last seen on, no longer mounted, says otherwise."""
        self.assertIsNone(self.since())  # seen: its mount is noted
        seen = retention._read_json(retention.VOLUMES)
        self.assertTrue(os.path.ismount(seen[self.name]))
        # As if it had lived on a volume mounted at a folder that is now gone.
        seen[self.name] = str(self.tmp / "mnt")
        retention._write_json(retention.VOLUMES, seen)
        self.src.rename(self.tmp / "elsewhere")

        self.assertIsNone(self.since())

    def test_a_source_gone_from_a_volume_still_mounted_counts(self):  # holds on BASE
        self.assertIsNone(self.since())
        self.src.rename(self.tmp / "elsewhere")

        self.assertIsNotNone(self.since())

    def test_a_watcher_notes_its_volume_and_forget_drops_it(self):
        (self.src / "a.mp4").write_bytes(b"x" * 2_000_000)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--preset", self.name, "--once", "--no-notify"])

        mount = retention._read_json(retention.VOLUMES)[self.name]
        self.assertTrue(os.path.ismount(mount))
        self.assertTrue(str(self.src).startswith(mount.rstrip("/") + "/"))
        retention.forget(self.name)
        self.assertNotIn(self.name, retention._read_json(retention.VOLUMES))

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
    def ask(self, *, clicked: bool, state: int = 0, logs: int = 0, problems=()):
        """Run `prune --ask` with the bookkeeping counts and the doctor's
        findings fixed, since the shared test state dir can hold debris from
        other tests, and no real notification may be sent."""
        calls: list[str] = []

        def fake_state(*, dry):
            calls.append(f"state dry={dry}")
            return state if dry else 0

        def fake_logs(*, dry):
            calls.append(f"logs dry={dry}")
            return logs if dry else 0

        with (
            mock.patch.object(cli, "ask", return_value=clicked) as ask,
            mock.patch.object(cli, "_prune_state", side_effect=fake_state),
            mock.patch.object(cli, "_prune_logs", side_effect=fake_logs),
            mock.patch.object(cli.doctor, "check_services", return_value=list(problems)),
            mock.patch.object(cli.doctor, "check_destinations", return_value=[]),
            mock.patch.object(cli, "notify") as notify,
        ):
            self.prune("--ask")
        ask.calls = calls
        ask.notify = notify
        return ask

    def test_a_failing_service_is_reported_weekly(self):
        """launchd restarts a service that dies on start forever, silently."""
        ask = self.ask(clicked=False, problems=[("error", "service job: last exit 1")])

        ask.notify.assert_called_once()
        self.assertIn("last exit 1", ask.notify.call_args.args[1])

        ask = self.ask(clicked=False, problems=[("ok", "service job: running")])
        ask.notify.assert_not_called()

    def test_bookkeeping_alone_waits_for_the_click(self):
        """Nothing is removed without the click, the tool's own state included."""
        ask = self.ask(clicked=False, state=3)

        ask.assert_called_once()
        self.assertNotIn("state dry=False", ask.calls)

        ask = self.ask(clicked=True, logs=2)
        self.assertIn("logs dry=False", ask.calls)

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


class TestPruneTemps(unittest.TestCase):
    def test_only_old_save_temps_are_removed(self):
        """A fresh .json.tmp may be a save in progress, about to be renamed
        into place by its writer; prune took every one."""
        fresh = STATE_DIR / f"done-fresh-{os.getpid()}.json.tmp"
        old = STATE_DIR / f"done-old-{os.getpid()}.json.tmp"
        for path in (fresh, old):
            path.write_text("{}")
            self.addCleanup(path.unlink, True)
        hours_ago = time.time() - 2 * 3600
        os.utime(old, (hours_ago, hours_ago))

        with contextlib.redirect_stdout(io.StringIO()):
            cli._prune_state(dry=False)

        self.assertTrue(fresh.exists(), "a save in progress was removed")
        self.assertFalse(old.exists(), "real debris was left")


class TestPruneLogs(unittest.TestCase):
    """Logs in the state folder: orphans removed after the retention window,
    oversized launchd .out logs cut to their tail."""

    def setUp(self):
        self.tag = f"logtest-{time.time_ns()}"
        self.made: list[Path] = []
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in self.made])

    def log(self, name: str, *, age_days: float = 0, size: int = 10) -> Path:
        path = STATE_DIR / name
        path.write_bytes(b"x" * (size - 1) + b"\n")
        then = time.time() - age_days * DAY
        os.utime(path, (then, then))
        self.made.append(path)
        return path

    def prune_logs(self, *, dry: bool) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            cli._prune_logs(dry=dry)

    def test_an_old_orphan_goes_and_everything_else_stays(self):
        save_preset(self.tag, {"action": "copy", "src": "/a", "to": "/b"})
        self.addCleanup(remove_preset, self.tag)
        orphan = self.log(f"{self.tag}-gone.log", age_days=40)
        recent = self.log(f"{self.tag}-recent.log", age_days=3)
        owned = self.log(f"{self.tag}.log", age_days=40)
        rotated = self.log(f"{self.tag}.log.1", age_days=40)
        tool = self.log(f"_{self.tag}.out.log", age_days=40)

        self.prune_logs(dry=True)
        self.assertTrue(orphan.exists(), "a dry run removed a log")

        self.prune_logs(dry=False)
        self.assertFalse(orphan.exists())
        for kept in (recent, owned, rotated, tool):
            self.assertTrue(kept.exists(), kept.name)

    def test_an_oversized_out_log_keeps_its_newest_whole_lines(self):
        save_preset(self.tag, {"action": "copy", "src": "/a", "to": "/b"})
        self.addCleanup(remove_preset, self.tag)
        path = self.log(f"{self.tag}.out.log")
        lines = [f"line {i:08d}\n".encode() for i in range(cli.OUT_LOG_MAX // 14 + 10)]
        path.write_bytes(b"".join(lines))
        inode = path.stat().st_ino

        self.prune_logs(dry=False)

        data = path.read_bytes()
        self.assertLessEqual(len(data), cli.OUT_LOG_KEEP)
        self.assertTrue(data.endswith(lines[-1]), "the newest line was lost")
        self.assertIn(data.split(b"\n", 1)[0] + b"\n", lines, "it starts mid-line")
        self.assertEqual(path.stat().st_ino, inode, "replaced: launchd writes elsewhere")


class TestLogFile(unittest.TestCase):
    def setUp(self):
        from folder_watch.support import log

        self.log_mod = log
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(log.setup_logging)

    def emitted(self, *, log_file, tty: bool) -> str:
        out = io.StringIO()
        out.isatty = lambda: tty  # type: ignore[method-assign]
        with contextlib.redirect_stdout(out):
            self.log_mod.setup_logging(log_file=log_file)
            self.log_mod.emit("probe", n=1)
        return out.getvalue()

    def test_with_a_log_file_and_no_terminal_events_go_to_the_file_once(self):
        """Under launchd stdout is the .out log, so printing too wrote every
        event twice."""
        f = self.tmp / "w.log"

        self.assertEqual(self.emitted(log_file=f, tty=False), "")
        self.assertIn("probe n=1", f.read_text())

    def test_a_terminal_and_runs_without_a_log_still_print(self):  # holds on BASE
        self.assertIn("probe", self.emitted(log_file=self.tmp / "w.log", tty=True))
        self.assertIn("probe", self.emitted(log_file=None, tty=False))

    def test_the_log_file_rotates(self):
        import logging.handlers

        self.log_mod.setup_logging(log_file=self.tmp / "w.log")
        handlers = [
            h for h in self.log_mod.log.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].maxBytes, self.log_mod.LOG_MAX_BYTES)
        self.assertGreater(handlers[0].backupCount, 0)


if __name__ == "__main__":
    unittest.main()
