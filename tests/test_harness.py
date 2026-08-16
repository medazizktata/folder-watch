#!/usr/bin/env python3
"""Tests for the test harness.

Unusual, and earned. `captured()` used to take the modules to patch as
arguments, so a test that named the wrong ones got an empty transcript and its
assertions passed against absent evidence — a green test proving nothing. That
is the worst failure a harness can have, because nothing about it looks wrong.

These check the harness reports what actually happened.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)
from _loopharness import _emit_importers, captured, count, fields

import folder_watch.support.log as log_mod


class TestCapture(unittest.TestCase):
    def test_it_finds_every_module_that_imported_emit(self):
        """Discovered from sys.modules rather than listed, so the next action
        added is captured without anyone remembering to say so."""
        # Make sure the ones that matter are actually imported first.
        for name in (
            "folder_watch.core.watch",
            "folder_watch.actions.copy",
            "folder_watch.actions.unzip",
            "folder_watch.actions.exec_cmd",
        ):
            importlib.import_module(name)

        found = {m.__name__ for m in _emit_importers()}

        self.assertIn("folder_watch.core.watch", found)
        self.assertIn("folder_watch.actions.exec_cmd", found)
        self.assertIn("folder_watch.actions.unzip", found)

    def test_every_action_module_that_logs_is_reachable(self):
        """The property that matters, stated over the real action registry:
        if an action emits, its lines land in the transcript."""
        from folder_watch.actions import ACTIONS

        for name, cls in ACTIONS.items():
            module = sys.modules[cls.__module__]
            with self.subTest(action=name):
                if not hasattr(module, "emit"):
                    continue  # this action logs only through the loop
                self.assertIn(
                    module,
                    _emit_importers(),
                    f"{name} logs, but its lines would not be captured",
                )

    def test_a_line_from_an_action_reaches_the_transcript(self):
        from folder_watch.actions.exec_cmd import ExecAction

        action = ExecAction("tool {path}", dry_run=True)
        with captured() as log:
            action.run(Path("/tmp/example.mp4"))

        self.assertEqual(count(log, "would_exec"), 1)

    def test_action_and_loop_lines_share_one_transcript_in_order(self):
        """The reason this is one capture and not two: the sequence is the
        evidence, and interleaving is what makes it readable."""
        import folder_watch.core.watch as watch_mod
        from folder_watch.actions.exec_cmd import ExecAction

        with captured() as log:
            watch_mod.emit("start", src="/tmp")
            ExecAction("tool {path}", dry_run=True).run(Path("/tmp/x.mp4"))
            watch_mod.emit("done", failed=0)

        self.assertEqual(
            [name for name, _ in log], ["start", "would_exec", "done"]
        )

    def test_nothing_escapes_to_stdout_while_capturing(self):
        import folder_watch.core.watch as watch_mod

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), captured() as log:
            watch_mod.emit("start", src="/tmp")

        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(count(log, "start"), 1)

    def test_emit_is_put_back_afterwards(self):
        import folder_watch.core.watch as watch_mod

        with captured():
            pass

        self.assertIs(watch_mod.emit, log_mod.emit)

    def test_a_failing_test_body_still_restores_emit(self):
        import folder_watch.core.watch as watch_mod

        with contextlib.suppress(RuntimeError), captured():
            raise RuntimeError("boom")

        self.assertIs(watch_mod.emit, log_mod.emit)

    def test_nested_captures_do_not_lose_the_original(self):
        import folder_watch.core.watch as watch_mod

        with captured() as outer:
            watch_mod.emit("start", n=1)
            with captured() as inner:
                watch_mod.emit("done", n=2)
            watch_mod.emit("done", n=3)

        self.assertEqual([n for n, _ in inner], ["done"])
        self.assertEqual([n for n, _ in outer], ["start", "done"])
        self.assertEqual(fields(outer, "done")[0]["n"], 3)
        self.assertIs(watch_mod.emit, log_mod.emit)


if __name__ == "__main__":
    unittest.main()
