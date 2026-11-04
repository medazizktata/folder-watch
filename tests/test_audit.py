"""The audit trail: what is recorded, who it is attributed to, and how long
it is kept. Each test writes to its own file."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import cli
from folder_watch.core import watch as watch_mod
from folder_watch.presets import load_raw_preset, remove_preset, save_preset
from folder_watch.support import audit, launchd
from folder_watch.support.log import emit


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(audit, "AUDIT", self.tmp / "audit.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XPC_SERVICE_NAME", None)
        audit.set_preset(None)
        self.addCleanup(audit.set_preset, None)

    def entries(self, kind=None):
        return [e for e in audit.read() if kind is None or e["kind"] == kind]


class TestRecording(AuditCase):
    def test_an_entry_has_time_kind_preset_trigger_and_details(self):
        audit.record("copied", preset="photos", name="a.jpg")

        (entry,) = self.entries()
        self.assertEqual(
            {k: entry[k] for k in ("kind", "preset", "trigger", "name")},
            {"kind": "copied", "preset": "photos", "trigger": "cli", "name": "a.jpg"},
        )
        datetime.fromisoformat(entry["ts"])

    def test_filters(self):
        audit.record("copied", preset="a")
        audit.record("fail", preset="b")
        old = json.dumps({"ts": "2020-01-01T00:00:00", "kind": "copied", "preset": "a"})
        with audit.AUDIT.open("a") as f:
            f.write(old + "\n")

        self.assertEqual([e["kind"] for e in audit.read(preset="b")], ["fail"])
        self.assertEqual(len(audit.read(kinds={"copied"})), 2)
        self.assertEqual(len(audit.read(since=audit.parse_since("7d"))), 2)

    def test_since_takes_durations_and_dates(self):
        now = datetime(2026, 11, 4, 12, 0)
        self.assertEqual(audit.parse_since("7d", now), now - timedelta(days=7))
        self.assertEqual(audit.parse_since("12h", now), now - timedelta(hours=12))
        self.assertEqual(audit.parse_since("2026-11-01", now), datetime(2026, 11, 1))
        with self.assertRaises(ValueError):
            audit.parse_since("soon", now)

    def test_a_failed_write_never_raises(self):
        """An audit line is not worth a failed copy."""
        with mock.patch.object(audit, "AUDIT", self.tmp / "a-dir"):
            (self.tmp / "a-dir").mkdir()
            audit.record("copied")


class TestTriggers(AuditCase):
    def test_how_the_process_was_started(self):
        for job, trigger in (
            (None, "cli"),
            ("com.folder-watch._weekly-prune", "weekly"),
            ("com.folder-watch.photos", "service"),
            ("com.apple.Something", "cli"),
        ):
            os.environ.pop("XPC_SERVICE_NAME", None)
            if job:
                os.environ["XPC_SERVICE_NAME"] = job
            self.assertEqual(audit.default_trigger(), trigger, job)

    def test_triggered_by_wins_and_is_undone(self):
        with audit.triggered_by("dashboard"):
            audit.record("a")
            with audit.triggered_by("automatic"):
                audit.record("b")
            audit.record("c")
        audit.record("d")

        self.assertEqual([e["trigger"] for e in self.entries()],
                         ["dashboard", "automatic", "dashboard", "cli"])


class TestWhatIsRecorded(AuditCase):
    def test_file_outcomes_and_moves_but_not_progress(self):
        audit.set_preset("photos")
        with contextlib.redirect_stdout(io.StringIO()):
            emit("copied", name="a.jpg", size=1)
            emit("progress", name="b.jpg", size=1)
            emit("error", reason="src_moved", src="/a", now="/b")
            emit("error", reason="src_missing", src="/a")

        self.assertEqual([e["kind"] for e in self.entries()], ["copied", "src_moved"])
        self.assertEqual(self.entries("copied")[0]["preset"], "photos")

    def test_preset_saves_say_what_changed_and_removals_are_kept(self):
        name = f"audit-{id(self)}"
        self.addCleanup(remove_preset, name)
        save_preset(name, {"action": "copy", "src": "/a", "to": "/b"})
        save_preset(name, {"action": "copy", "src": "/c", "to": "/b"})
        remove_preset(name)

        created, changed = self.entries("preset_saved")
        self.assertTrue(created["created"])
        self.assertEqual(changed["changes"], {"src": ["/a", "/c"]})
        self.assertEqual(audit.describe(changed), "src: /a -> /c")
        self.assertEqual(len(self.entries("preset_removed")), 1)

    def test_service_operations(self):
        name = f"audit-svc-{id(self)}"
        plist = launchd.service_plist_path(name)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        self.addCleanup(plist.unlink, True)
        with (
            mock.patch.object(launchd, "_launchctl") as ctl,
            mock.patch.object(launchd.subprocess, "run"),
        ):
            ctl.return_value.returncode = 0
            launchd.pause_service(preset=name)
            launchd.resume_service(preset=name)
            launchd.uninstall_service(preset=name)

        self.assertEqual(
            [e["kind"] for e in self.entries()],
            ["service_paused", "service_resumed", "service_uninstalled"],
        )

    def test_a_followed_move_says_whether_a_person_chose_it(self):
        name = f"audit-follow-{id(self)}"
        src = self.tmp / "src"
        src.mkdir()
        self.addCleanup(remove_preset, name)
        moved = self.tmp / "moved"
        for follow_moves, expected in ((True, "automatic"), (False, "notification")):
            save_preset(name, {"action": "copy", "src": str(src), "to": "/d",
                               "follow_moves": follow_moves})
            (src if src.exists() else moved).rename(moved)
            with (
                mock.patch.object(watch_mod, "ask", return_value=True),
                mock.patch.object(watch_mod, "notify"),
                mock.patch.object(watch_mod.os, "execv"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                watch_mod.offer_to_follow(name, src, moved)
            moved.rename(src)
            self.assertEqual(self.entries("src_followed")[-1]["trigger"], expected)
            self.assertEqual(load_raw_preset(name)["src"], str(moved))


class TestRetention(AuditCase):
    def test_a_year_is_kept_and_undated_lines_are_never_dropped(self):
        now = time.time()
        old = datetime.fromtimestamp(now - 400 * 86400).isoformat(timespec="seconds")
        recent = datetime.fromtimestamp(now - 10 * 86400).isoformat(timespec="seconds")
        lines = [json.dumps({"ts": old, "kind": "copied"}),
                 json.dumps({"ts": recent, "kind": "copied"}),
                 "not json"]
        audit.AUDIT.write_text("\n".join(lines) + "\n")

        self.assertEqual(audit.trim(now=now, dry=True), 1)
        self.assertEqual(len(audit.AUDIT.read_text().splitlines()), 3, "dry run wrote")
        self.assertEqual(audit.trim(now=now, dry=False), 1)
        self.assertEqual(audit.AUDIT.read_text().splitlines(), lines[1:])

    def test_prune_trims_it_and_records_that_it_did(self):
        old = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
        audit.AUDIT.write_text(json.dumps({"ts": old, "kind": "copied"}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            cli._prune_state(dry=False)

        (entry,) = self.entries()
        self.assertEqual(entry["kind"], "state_pruned")
        self.assertEqual(entry["audit_entries"], 1)


class TestAuditCommand(AuditCase):
    def run_audit(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run_audit_cli(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_lines_and_filters(self):
        audit.record("copied", preset="photos", name="a.jpg")
        audit.record("fail", preset="inbox", name="b.zip")

        rc, out, _ = self.run_audit("--preset", "photos")

        self.assertEqual(rc, 0)
        self.assertIn("copied", out)
        self.assertIn("name=a.jpg", out)
        self.assertNotIn("b.zip", out)
        self.assertIn("1 entr(ies)", out)

    def test_a_bad_since_is_an_error(self):
        self.assertEqual(self.run_audit("--since", "soon")[0], 2)


if __name__ == "__main__":
    unittest.main()
