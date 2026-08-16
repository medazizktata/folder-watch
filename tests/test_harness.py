#!/usr/bin/env python3
"""Tests for the test harness.

Unusual, and earned. `captured()` used to take the modules to patch as
arguments, so a test that named the wrong ones got an empty transcript and its
assertions passed against absent evidence — a green test proving nothing. That
is the worst failure a harness can have, because nothing about it looks wrong.

These check the harness reports what actually happened.
"""
from __future__ import annotations

import ast
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


class TestNoSleepAsSynchronisation(unittest.TestCase):
    """The smoke suite spawns real watchers, so it is the one place that can
    be tempted to sleep and hope.

    Every such sleep is a guess about someone else's machine. It looks generous
    right up until a cold CI runner disagrees, and then it fails somewhere
    nobody can reproduce — which is exactly how a working watcher was reported
    as broken on 3.13 while passing on 3.10 from a commit that touched neither.

    The rule this enforces: a sleep may only be a poll interval, i.e. sit
    inside a loop with a real condition and a deadline. A sleep on its own is
    waiting for something to have happened, and the thing to do instead is
    wait for the signal that says it did.
    """

    SUITES = ("test_addons_smoke.py",)

    @staticmethod
    def _sleeps(tree):
        """(every sleep call, those lexically inside a while loop)."""
        every, in_loop = set(), set()

        def is_sleep(node):
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
            )

        for node in ast.walk(tree):
            if is_sleep(node):
                every.add(node.lineno)
            if isinstance(node, ast.While):
                for inner in ast.walk(node):
                    if is_sleep(inner):
                        in_loop.add(inner.lineno)
        return every, in_loop

    def test_no_sleep_stands_alone(self):
        for name in self.SUITES:
            path = Path(__file__).parent / name
            with self.subTest(suite=name):
                every, in_loop = self._sleeps(ast.parse(path.read_text()))
                bare = sorted(every - in_loop)
                self.assertEqual(
                    bare,
                    [],
                    f"{name}: sleep used to wait for something at line(s) "
                    f"{bare} — poll for the signal instead",
                )

    def test_the_check_can_actually_fail(self):
        """A guard that cannot fail is decoration."""
        tree = ast.parse(
            "import time\n"
            "while x:\n"
            "    time.sleep(0.1)\n"   # a poll interval — allowed
            "time.sleep(0.5)\n"       # waiting and hoping — not
        )
        every, in_loop = self._sleeps(tree)

        self.assertEqual(sorted(every - in_loop), [4])


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
