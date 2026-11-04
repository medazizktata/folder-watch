"""The weekly digest, counted by the date on each log line."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest import mock

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import cli, digest
from folder_watch.presets import remove_preset, save_preset
from folder_watch.support import log as log_mod
from folder_watch.support.paths import STATE_DIR

TODAY = date(2026, 11, 8)


class TestCounting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name: str, *lines: str) -> Path:
        path = self.tmp / name
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_only_lines_inside_the_window_count(self):
        log = self.write(
            "p.log",
            "2026-11-01 23:59:59 INFO copied name=too-old.mp4",
            "2026-11-02 00:00:01 INFO copied name=a.mp4",
            "2026-11-08 10:00:00 INFO fail name=b.mp4 reason=copy_error",
            "2026-11-08 10:00:01 INFO progress name=c.mp4 size=1",
        )

        counts = digest.count_events([log], since=date(2026, 11, 2))

        self.assertEqual(counts, Counter({"copied": 1, "fail": 1, "progress": 1}))

    def test_json_lines_count_by_their_timestamp(self):
        log = self.write(
            "p.log",
            json.dumps({"ts": "2026-11-05T09:00:00", "event": "extracted", "name": "z"}),
            json.dumps({"ts": "2026-10-01T09:00:00", "event": "extracted", "name": "y"}),
        )

        self.assertEqual(
            digest.count_events([log], since=date(2026, 11, 2)),
            Counter({"extracted": 1}),
        )

    def test_undated_lines_from_before_dates_were_logged_are_skipped(self):
        log = self.write(
            "p.log", "09:00:00 INFO copied name=a.mp4", '{"event": "copied"}'
        )

        self.assertEqual(digest.count_events([log], since=date(2000, 1, 1)), Counter())

    def test_rotated_logs_are_counted_and_other_logs_are_not(self):
        name = f"digest-{id(self)}"
        save_preset(name, {"action": "copy", "src": "/a", "to": "/b"})
        self.addCleanup(remove_preset, name)
        paths = {
            f"{name}.log": "2026-11-07 10:00:00 INFO copied name=a.mp4",
            f"{name}.log.1": "2026-11-06 10:00:00 INFO copied name=b.mp4",
            f"{name}.out.log": "2026-11-06 10:00:00 INFO copied name=dup.mp4",
        }
        for file, line in paths.items():
            (STATE_DIR / file).write_text(line + "\n")
            self.addCleanup((STATE_DIR / file).unlink, True)

        counts = digest.week(today=TODAY)[name]

        self.assertEqual(counts["copied"], 2, "rotated copy missed, or .out.log counted")


class TestSummary(unittest.TestCase):
    def test_nothing_happened_is_none(self):
        self.assertIsNone(digest.summary({"a": Counter(), "b": Counter({"progress": 9})}))

    def test_totals_failures_and_the_busiest_presets(self):
        text = digest.summary({
            "photos": Counter({"copied": 30}),
            "inbox": Counter({"extracted": 4, "fail": 1, "give_up": 1}),
            "quiet": Counter(),
        })

        self.assertEqual(text, "copied 30, extracted 4, 2 failed (photos, inbox)")

    def test_more_than_three_presets_are_counted_not_listed(self):
        text = digest.summary({f"p{i}": Counter({"copied": 10 - i}) for i in range(5)})

        self.assertTrue(text.endswith("(p0, p1, p2 +2)"), text)


class TestWeeklyNotification(unittest.TestCase):
    def run_weekly(self, week):
        with (
            mock.patch.object(cli.doctor, "check_services", return_value=[]),
            mock.patch.object(cli.doctor, "check_destinations", return_value=[]),
            mock.patch.object(cli.digest, "week", return_value=week),
            mock.patch.object(cli, "_prune_state", return_value=0),
            mock.patch.object(cli, "_prune_presets", return_value=[]),
            mock.patch.object(cli, "_prune_logs", return_value=0),
            mock.patch.object(cli, "notify") as notify,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.run_prune_cli(["--ask"])
        return notify

    def test_a_week_with_activity_is_announced(self):
        notify = self.run_weekly({"photos": Counter({"copied": 3})})

        notify.assert_called_once()
        self.assertEqual(
            notify.call_args.args, ("folder-watch this week", "copied 3 (photos)")
        )

    def test_a_quiet_week_sends_nothing(self):
        self.run_weekly({"photos": Counter()}).assert_not_called()


class TestDatedLogLines(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(log_mod.setup_logging)
        self.addCleanup(log_mod.set_json_mode, False)

    def logged(self, *, json_mode: bool) -> str:
        path = self.tmp / "w.log"
        log_mod.set_json_mode(json_mode)
        with contextlib.redirect_stdout(io.StringIO()):
            log_mod.setup_logging(log_file=path, json_mode=json_mode)
            log_mod.emit("copied", name="a.mp4")
        return path.read_text().strip()

    def test_plain_lines_start_with_the_date(self):
        self.assertRegex(self.logged(json_mode=False),
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO copied name=a\.mp4$")

    def test_json_lines_carry_a_timestamp(self):
        event = json.loads(self.logged(json_mode=True))
        self.assertEqual(event["event"], "copied")
        self.assertRegex(event["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()
