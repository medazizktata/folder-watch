#!/usr/bin/env python3
"""Tests for the exec action.

Two things carry the weight here. The first is that a filename can never
become a command — the substitution happens into already-split argv tokens, and
an export called `note; rm -rf ~.mp4` is not hypothetical, it is a Tuesday. The
second is the exit-code contract, which is what lets someone else's program
reach the retry queue without this tool guessing what its failures mean.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)
from _loopharness import captured, count, fields

import folder_watch.actions.exec_cmd as exec_mod
import folder_watch.core.watch as watch_mod
from folder_watch.actions.exec_cmd import EX_TEMPFAIL, ExecAction
from folder_watch.support.retry import RetryQueue
from folder_watch.support.state import DoneStore

PY = sys.executable


def script(body: str) -> str:
    """A command template that runs `body` with this interpreter."""
    return f'{PY} -c "{body}"'


class ExecCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.src = self.tmp / "src"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_file(self, name="clip.mp4", size=10) -> Path:
        f = self.src / name
        f.write_bytes(b"x" * size)
        return f

    def run_action(self, action, path):
        with captured(exec_mod) as log:
            result = action.run(path)
        return result, log


class TestTemplate(ExecCase):
    """Turning --cmd plus a file into argv."""

    def test_every_placeholder_is_substituted(self):
        sub = self.src / "shots"
        sub.mkdir()
        f = sub / "take-3.mov"
        f.write_bytes(b"x")
        action = ExecAction("tool {path} {dir} {name} {stem} {ext} {rel}",
                            src_root=self.src)

        argv = action.build_argv(f)

        self.assertEqual(
            argv,
            [
                "tool", str(f), str(sub), "take-3.mov",
                "take-3", "mov", "shots/take-3.mov",
            ],
        )

    def test_rel_falls_back_to_the_name_without_a_root(self):
        f = self.make_file("clip.mp4")
        action = ExecAction("tool {rel}")

        self.assertEqual(action.build_argv(f), ["tool", "clip.mp4"])

    def test_a_filename_with_spaces_stays_one_argument(self):
        f = self.make_file("two words.mp4")
        action = ExecAction("tool {path}")

        argv = action.build_argv(f)

        self.assertEqual(len(argv), 2)
        self.assertEqual(argv[1], str(f))

    def test_a_filename_cannot_become_a_second_command(self):
        """The whole reason substitution happens after the split, not before.

        Split-after-substitute would turn this one filename into three argv
        entries and hand `rm` a plausible-looking argument list.
        """
        f = self.make_file("note; rm -rf ~.mp4")
        action = ExecAction("tool {path}")

        argv = action.build_argv(f)

        self.assertEqual(len(argv), 2, "the filename was split into arguments")
        self.assertEqual(argv[1], str(f))
        self.assertNotIn("rm", argv)

    def test_quotes_and_substitutions_in_a_filename_are_inert(self):
        for hostile in ('quote".mp4', "back`tick`.mp4", "dollar$(id).mp4"):
            with self.subTest(name=hostile):
                f = self.make_file(hostile)
                argv = ExecAction("tool {path}").build_argv(f)
                self.assertEqual(len(argv), 2)
                self.assertEqual(argv[1], str(f))

    def test_an_empty_command_is_refused_up_front(self):
        for bad in ("", "   "):
            with self.subTest(cmd=bad), self.assertRaises(ValueError):
                ExecAction(bad)

    def test_the_command_is_part_of_the_done_key(self):
        """Editing --cmd should re-process files the old command handled —
        that is nearly always what changing it meant."""
        self.assertEqual(ExecAction("a {path}").done_mode(), "a {path}")
        self.assertNotEqual(
            ExecAction("a {path}").done_mode(), ExecAction("b {path}").done_mode()
        )

    def test_it_cannot_tell_whether_output_exists(self):
        """None, not False — an arbitrary command's output is unknowable, and
        False would make --pending claim every file was missed."""
        self.assertIsNone(ExecAction("tool {path}").output_exists(self.make_file()))


class TestExitCodes(ExecCase):
    """The contract between someone else's program and the retry queue."""

    def test_a_clean_exit_is_success(self):
        f = self.make_file()
        action = ExecAction(script("pass"))

        result, log = self.run_action(action, f)

        self.assertEqual(result, "ran")
        self.assertEqual(fields(log, "ran")[0]["name"], f.name)

    def test_seventy_five_asks_to_be_retried_later(self):
        """EX_TEMPFAIL from sysexits.h — "not really an error, reattempt"."""
        f = self.make_file()
        action = ExecAction(script(f"import sys; sys.exit({EX_TEMPFAIL})"))

        result, log = self.run_action(action, f)

        self.assertEqual(result, "retry")
        self.assertEqual(fields(log, "retry_requested")[0]["exit"], EX_TEMPFAIL)
        self.assertEqual(count(log, "fail"), 0)

    def test_any_other_nonzero_exit_is_permanent(self):
        f = self.make_file()
        action = ExecAction(script("import sys; sys.exit(3)"))

        result, log = self.run_action(action, f)

        self.assertEqual(result, "fail")
        failed = fields(log, "fail")[0]
        self.assertEqual(failed["exit"], 3)
        self.assertEqual(failed["reason"], "nonzero_exit")

    def test_the_command_own_diagnostic_reaches_the_log(self):
        """A bare exit code explains nothing; the program's own message does."""
        f = self.make_file()
        action = ExecAction(
            script("import sys; sys.stderr.write('codec not supported'); sys.exit(1)")
        )

        _result, log = self.run_action(action, f)

        self.assertIn("codec not supported", fields(log, "fail")[0]["output"])

    def test_a_torrent_of_output_is_trimmed(self):
        f = self.make_file()
        action = ExecAction(
            script("import sys; sys.stderr.write('x'*100000); sys.exit(1)")
        )

        _result, log = self.run_action(action, f)

        self.assertLess(len(fields(log, "fail")[0]["output"]), 1000)

    def test_a_missing_command_fails_without_retrying_forever(self):
        f = self.make_file()
        action = ExecAction("definitely-not-a-real-binary-xyz {path}")

        result, log = self.run_action(action, f)

        self.assertEqual(result, "fail")
        self.assertEqual(fields(log, "fail")[0]["reason"], "command_not_found")

    def test_a_command_that_hangs_is_killed_and_reported(self):
        """A command that never returns stops the watch loop, silently and
        forever — which is why there is a default timeout rather than an
        invitation to think about it later."""
        f = self.make_file()
        action = ExecAction(script("import time; time.sleep(30)"), timeout=0.5)

        result, log = self.run_action(action, f)

        self.assertEqual(result, "fail")
        self.assertEqual(fields(log, "fail")[0]["reason"], "timeout")

    def test_a_killed_command_is_not_retried(self):
        """Its state is unknown — it may have half-done the work. Reporting
        that beats repeating it."""
        f = self.make_file()
        action = ExecAction(script("import time; time.sleep(30)"), timeout=0.5)

        result, _log = self.run_action(action, f)

        self.assertNotEqual(result, "retry")

    def test_it_runs_beside_the_file_it_was_given(self):
        """So a command writing a relative output puts it next to the input.
        Inheriting the watcher's cwd means / under launchd."""
        f = self.make_file()
        action = ExecAction(script("open('marker','w').write('here')"))

        self.run_action(action, f)

        self.assertTrue((self.src / "marker").is_file())

    def test_a_dry_run_shows_the_command_without_running_it(self):
        f = self.make_file()
        action = ExecAction(script("open('never','w').write('x')"), dry_run=True)

        result, log = self.run_action(action, f)

        self.assertEqual(result, "would_exec")
        self.assertFalse((self.src / "never").exists())
        self.assertIn(PY, fields(log, "would_exec")[0]["argv"])


class TestThroughTheWatchLoop(ExecCase):
    """The point of the action: reaching machinery it did not have to build."""

    def test_a_transient_failure_lands_in_the_retry_queue(self):
        f = self.make_file()
        retries = RetryQueue(self.tmp / "retry.json")
        action = ExecAction(script(f"import sys; sys.exit({EX_TEMPFAIL})"))

        with captured(exec_mod) as log:
            result = watch_mod.apply_action(
                f, action, state={}, stable_needed=1, retries=retries
            )

        self.assertEqual(result, "retry")
        self.assertEqual(len(retries), 1)
        self.assertEqual(fields(log, "retry_scheduled")[0]["attempt"], 1)

    def test_success_is_logged_once_by_the_action_itself(self):
        """`ran` carries the command and the duration; a generic `result` line
        on top of it would say less, twice."""
        f = self.make_file()
        action = ExecAction(script("pass"))

        with captured(exec_mod) as log:
            watch_mod.apply_action(f, action, state={}, stable_needed=1)

        self.assertEqual(count(log, "ran"), 1)
        self.assertEqual(count(log, "result"), 0)

    def test_a_preview_is_never_recorded_as_done(self):
        f = self.make_file()
        store = DoneStore(self.tmp / "done.json")
        action = ExecAction(script("pass"), dry_run=True)
        state: dict[str, dict] = {}

        with captured(exec_mod):
            watch_mod.apply_action(
                f, action, state=state, stable_needed=1, done_store=store
            )

        self.assertEqual(len(store), 0)
        self.assertEqual(state, {})

    def test_the_same_file_is_not_processed_twice(self):
        f = self.make_file()
        store = DoneStore(self.tmp / "done.json")
        action = ExecAction(script("import time; open('runs','a').write('.')"))
        state: dict[str, dict] = {}

        with captured(exec_mod):
            for _ in range(3):
                watch_mod.apply_action(
                    f, action, state=state, stable_needed=1, done_store=store
                )

        self.assertEqual((self.src / "runs").read_text(), ".")

    def test_changing_the_command_processes_the_file_again(self):
        f = self.make_file()
        store = DoneStore(self.tmp / "done.json")
        first = ExecAction(script("open('a','w').write('x')"))
        second = ExecAction(script("open('b','w').write('x')"))

        with captured(exec_mod):
            watch_mod.apply_action(
                f, first, state={}, stable_needed=1, done_store=store
            )
            watch_mod.apply_action(
                f, second, state={}, stable_needed=1, done_store=store
            )

        self.assertTrue((self.src / "a").is_file())
        self.assertTrue((self.src / "b").is_file(), "the new command never ran")


class TestCli(ExecCase):
    def fw(self, *argv):
        return subprocess.run(
            [PY, "-m", "folder_watch", *argv],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

    def test_exec_without_a_command_says_so(self):
        result = self.fw("exec", str(self.src), "--once")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--cmd", result.stderr)

    def test_a_single_pass_runs_the_command_on_what_is_there(self):
        self.make_file("a.txt")
        self.make_file("b.txt")

        result = self.fw(
            "exec", str(self.src), "--ext", "*", "--min-bytes", "1",
            # Quoted as a unit: --cmd is shlex-split, so the inner quotes
            # have to survive that split the same as they would for xargs.
            "--cmd", script("open('{stem}.done','w').write('x')"),
            "--once", "--catch-up", "--no-done-store",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.src / "a.done").is_file())
        self.assertTrue((self.src / "b.done").is_file())

    def test_a_failing_command_sets_the_exit_status(self):
        """So cron and CI can tell, the same as for a failed copy."""
        self.make_file("a.txt")

        result = self.fw(
            "exec", str(self.src), "--ext", "*", "--min-bytes", "1",
            "--cmd", script("import sys; sys.exit(1)"),
            "--once", "--catch-up", "--no-done-store",
        )

        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
