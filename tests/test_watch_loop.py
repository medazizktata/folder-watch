#!/usr/bin/env python3
"""Tests for the watch loops themselves: settle -> queue -> retry.

Every audit round found its bugs in core/watch.py, and every one of them lived
on a path nothing exercised: the stall abandonment, the writer that let go
without an event, the same-length rewrite, the poll backend that never
serviced its retry queue. These cover those paths directly.

The loops never return on their own and their timeouts run to a day, so they
are driven through the doubles in _loopharness: a virtual clock whose sleep
advances time and calls back into the test, and an observer stub whose handler
the tests invoke by hand.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)
from _loopharness import (
    Clock,
    Event,
    FakeObserver,
    OpenScript,
    StopLoop,
    captured,
    count,
    fields,
    kinds,
    patched,
)

import folder_watch.core.watch as watch_mod
from folder_watch.actions.base import Action
from folder_watch.support.retry import MAX_PER_SCAN, RetryQueue
from folder_watch.support.state import DoneStore, file_identity


class ScriptedAction(Action):
    """An action whose result each call is dictated by the test."""

    name = "scripted"

    def __init__(self, *results, dest_ready=True, output=None):
        self.results = list(results)
        self.calls: list[Path] = []
        self.dest_ready = dest_ready
        self.probes = 0
        self._output = output

    def run(self, path: Path) -> str:
        self.calls.append(path)
        result = self.results.pop(0) if self.results else "copied"
        if isinstance(result, Exception):
            raise result
        return result

    def destination_ready(self) -> bool:
        self.probes += 1
        return self.dest_ready

    def output_exists(self, src: Path):
        return self._output


class LoopCase(unittest.TestCase):
    """Shared scratch dirs; nothing here reads or writes outside them."""

    def setUp(self):
        # Resolved, because the event handler compares parents literally and
        # the platform temp dir is itself a symlink.
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.src = self.tmp / "src"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def queue(self, path: Path, retries: RetryQueue, *, due=True) -> dict:
        size, mtime_ns = file_identity(path)
        entry = retries.schedule(
            path, action="scripted", reason="transient", size=size, mtime_ns=mtime_ns
        )
        assert entry is not None
        if due:
            entry["next_due"] = 0.0
        return entry

    def make_file(self, name="clip.mp4", size=100) -> Path:
        f = self.src / name
        f.write_bytes(b"x" * size)
        return f


class TestServiceRetries(LoopCase):
    """The queue drain shared by both backends.

    The poll backend went without this entirely: it re-ran permanent failures
    on every scan, with no backoff and no give-up budget.
    """

    def setUp(self):
        super().setUp()
        self.retries = RetryQueue(self.tmp / "retry.json")
        self.state: dict[str, dict] = {}

    def service(self, action, **kw):
        with captured() as log:
            flag = watch_mod.service_retries(
                self.retries,
                action,
                state=self.state,
                stable_needed=1,
                **kw,
            )
        return flag, log

    def test_a_due_file_is_retried_and_leaves_the_queue(self):
        f = self.make_file()
        self.queue(f, self.retries)
        action = ScriptedAction("copied")

        _flag, log = self.service(action)

        self.assertEqual(action.calls, [f])
        self.assertEqual(len(self.retries), 0)
        self.assertEqual(fields(log, "retry")[0]["attempt"], 1)

    def test_an_absent_destination_costs_one_probe_for_the_whole_queue(self):
        """500 files behind an unmounted drive must not cost 500 attempts."""
        for n in range(3):
            self.queue(self.make_file(f"clip{n}.mp4"), self.retries)
        action = ScriptedAction(dest_ready=False)

        flag, log = self.service(action)

        self.assertEqual(action.probes, 1)
        self.assertEqual(action.calls, [])
        self.assertTrue(flag, "caller was not told the notice had been given")
        self.assertEqual(count(log, "retry_waiting"), 1)
        self.assertEqual(fields(log, "retry_waiting")[0]["reason"], "dest_away")

    def test_the_absent_destination_notice_is_not_repeated(self):
        self.queue(self.make_file(), self.retries)
        action = ScriptedAction(dest_ready=False)

        _flag, log = self.service(action, dest_away_logged=True)

        self.assertEqual(count(log, "retry_waiting"), 0)

    def test_the_notice_arms_again_once_the_destination_returns(self):
        f = self.make_file()
        self.queue(f, self.retries)
        action = ScriptedAction("copied")

        flag, _log = self.service(action, dest_away_logged=True)

        self.assertFalse(flag, "flag stayed set after the destination came back")
        self.assertEqual(action.calls, [f])

    def test_deferring_does_not_spend_an_attempt(self):
        f = self.make_file()
        entry = self.queue(f, self.retries)
        self.assertEqual(entry["attempts"], 1)

        self.service(ScriptedAction(dest_ready=False))

        self.assertEqual(self.retries._data[str(f)]["attempts"], 1)
        self.assertGreater(self.retries._data[str(f)]["next_due"], 0.0)

    def test_a_file_that_vanished_is_dropped_not_retried(self):
        f = self.make_file()
        self.queue(f, self.retries)
        f.unlink()
        action = ScriptedAction()

        self.service(action)

        self.assertEqual(action.calls, [])
        self.assertEqual(len(self.retries), 0)

    def test_a_rewritten_file_goes_back_to_settle_instead_of_being_copied(self):
        """The queue holds the bytes that failed. New bytes must not use it.

        Retries deliberately skip the settle loop, so applying one to a file
        that has since been rewritten would copy a half-written source.
        """
        f = self.make_file()
        self.queue(f, self.retries)
        f.write_bytes(b"y" * 4000)
        action = ScriptedAction()
        requeued: list[Path] = []

        self.service(action, requeue=requeued.append)

        self.assertEqual(action.calls, [], "stale bytes were copied")
        self.assertEqual(requeued, [f])
        self.assertEqual(len(self.retries), 0)

    def test_the_give_up_budget_is_announced_then_forgotten(self):
        retries = RetryQueue(self.tmp / "short.json", give_up_after=3600.0)
        f = self.make_file()
        size, mtime_ns = file_identity(f)
        entry = retries.schedule(
            f, action="scripted", reason="transient", size=size, mtime_ns=mtime_ns
        )
        entry["first_failed"] = entry["first_failed"] - 7200

        with captured() as log:
            watch_mod.service_retries(
                retries, ScriptedAction(), state=self.state, stable_needed=1
            )

        gave_up = fields(log, "give_up")
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(gave_up[0]["reason"], "retry_exhausted")
        self.assertEqual(gave_up[0]["hours"], 1)
        self.assertEqual(len(retries), 0)

    def test_a_backlog_is_drained_in_bounded_batches(self):
        """A drive coming back with a thousand files queued must not stampede."""
        for n in range(MAX_PER_SCAN + 5):
            self.queue(self.make_file(f"clip{n}.mp4"), self.retries)
        action = ScriptedAction()

        self.service(action)

        self.assertEqual(len(action.calls), MAX_PER_SCAN)
        self.assertEqual(len(self.retries), 5)


class TestApplyAction(LoopCase):
    """What happens to a file once it has settled."""

    def setUp(self):
        super().setUp()
        self.state: dict[str, dict] = {}
        self.retries = RetryQueue(self.tmp / "retry.json")
        self.store = DoneStore(self.tmp / "done.json")

    def apply(self, path, action, **kw):
        with captured() as log:
            result = watch_mod.apply_action(
                path, action, state=self.state, stable_needed=1, **kw
            )
        return result, log

    def test_a_transient_failure_is_queued_with_a_backoff(self):
        f = self.make_file()

        result, log = self.apply(f, ScriptedAction("retry"), retries=self.retries)

        self.assertEqual(result, "retry")
        self.assertEqual(len(self.retries), 1)
        scheduled = fields(log, "retry_scheduled")
        self.assertEqual(scheduled[0]["attempt"], 1)
        self.assertGreater(scheduled[0]["in_s"], 0)

    def test_a_full_queue_says_so_rather_than_dropping_the_file_silently(self):
        f = self.make_file()
        full = RetryQueue(self.tmp / "full.json", max_entries=0)

        result, log = self.apply(f, ScriptedAction("retry"), retries=full)

        self.assertEqual(result, "retry")
        self.assertEqual(
            [w["reason"] for w in fields(log, "warn")], ["retry_queue_full"]
        )

    def test_a_terminal_failure_clears_any_queued_entry(self):
        """Nothing left to wait for, so nothing left to retry."""
        f = self.make_file()
        self.queue(f, self.retries)

        result, _log = self.apply(f, ScriptedAction("fail"), retries=self.retries)

        self.assertEqual(result, "fail")
        self.assertEqual(len(self.retries), 0)

    def test_success_clears_the_queue_and_records_the_done_key(self):
        f = self.make_file()
        self.queue(f, self.retries)

        result, _log = self.apply(
            f, ScriptedAction("copied"), retries=self.retries, done_store=self.store
        )

        self.assertEqual(result, "copied")
        self.assertEqual(len(self.retries), 0)
        self.assertEqual(len(self.store), 1)
        self.assertTrue(self.state[str(f)]["done"])

    def test_a_recorded_file_never_reaches_the_action(self):
        f = self.make_file()
        size, mtime_ns = file_identity(f)
        key = DoneStore.make_key("scripted", f, size=size, mtime_ns=mtime_ns)
        self.store.mark(key, name=f.name)
        action = ScriptedAction()

        result, log = self.apply(f, action, done_store=self.store)

        self.assertEqual(result, "skip")
        self.assertEqual(action.calls, [])
        self.assertEqual(fields(log, "skip")[0]["reason"], "done_store")

    def test_a_dry_run_result_is_not_persisted(self):
        f = self.make_file()

        result, _log = self.apply(
            f, ScriptedAction("would_copy"), done_store=self.store
        )

        self.assertEqual(result, "would_copy")
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.state, {})

    def test_an_action_that_raises_becomes_an_error_not_a_crash(self):
        f = self.make_file()

        result, log = self.apply(f, ScriptedAction(RuntimeError("disk on fire")))

        self.assertEqual(result, "error")
        self.assertIn("disk on fire", fields(log, "error")[0]["err"])

    def test_an_unrecognised_result_is_reported_verbatim(self):
        f = self.make_file()

        result, log = self.apply(f, ScriptedAction("converted-ish"))

        self.assertEqual(result, "converted-ish")
        self.assertEqual(fields(log, "result")[0]["status"], "converted-ish")

    def test_an_identical_file_is_skipped_without_touching_the_action(self):
        f = self.make_file()
        size, mtime_ns = file_identity(f)
        self.state[str(f)] = {"size": size, "mtime_ns": mtime_ns, "done": True}
        action = ScriptedAction()

        result, _log = self.apply(f, action)

        self.assertIsNone(result)
        self.assertEqual(action.calls, [])

    def test_a_same_length_rewrite_is_not_mistaken_for_the_same_file(self):
        """Size alone said "done" and returned in silence, so the destination
        kept the stale copy with nothing in the log to say why."""
        f = self.make_file(size=100)
        size, mtime_ns = file_identity(f)
        self.state[str(f)] = {"size": size, "mtime_ns": mtime_ns - 1, "done": True}
        action = ScriptedAction()

        result, _log = self.apply(f, action)

        self.assertEqual(result, "copied")
        self.assertEqual(action.calls, [f])

    def test_a_file_that_disappeared_first_is_not_an_error(self):
        result, log = self.apply(self.src / "never-existed.mp4", ScriptedAction())

        self.assertIsNone(result)
        self.assertEqual(log, [])

    def test_a_skip_is_announced_with_the_size_that_was_skipped(self):
        f = self.make_file(size=100)

        result, log = self.apply(f, ScriptedAction("skip"))

        self.assertEqual(result, "skip")
        self.assertEqual(fields(log, "skip")[0]["size"], 100)

    def test_an_error_from_the_action_is_reported_and_returned(self):
        f = self.make_file()

        result, log = self.apply(f, ScriptedAction("error"))

        self.assertEqual(result, "error")
        self.assertEqual(fields(log, "result")[0]["status"], "error")


class TestProcessFile(LoopCase):
    """Settle-then-act, in both the blocking and non-blocking forms."""

    def process(self, path, action, *, clock=None, is_open=None, **kw):
        opts = {
            "state": kw.pop("state", {}),
            "min_bytes": kw.pop("min_bytes", 1),
            "stable_needed": kw.pop("stable_needed", 1),
            "settle_interval": kw.pop("settle_interval", 0.5),
        }
        opts.update(kw)
        with captured() as log, patched(clock, is_open):
            result = watch_mod.process_file(path, action, **opts)
        return result, log

    def test_an_open_file_is_left_for_the_next_pass(self):
        """Non-blocking means non-blocking: an in-progress write is not waited
        on, it is simply not this pass's problem."""
        f = self.make_file()
        action = ScriptedAction()
        probe = OpenScript(1)

        result, _log = self.process(
            f, action, clock=Clock(stop_after=50), is_open=probe, wait=False
        )

        self.assertIsNone(result)
        self.assertEqual(action.calls, [])
        self.assertEqual(len(probe.probes), 1, "paid for lsof more than once")

    def test_a_file_under_the_size_floor_says_why_it_was_ignored(self):
        """copy defaults to a one-megabyte floor; without this line a small
        file vanishes from the log with no explanation at all."""
        f = self.make_file(size=10)
        action = ScriptedAction()

        result, log = self.process(
            f,
            action,
            clock=Clock(stop_after=50),
            is_open=OpenScript(0),
            wait=False,
            min_bytes=1_000_000,
        )

        self.assertIsNone(result)
        self.assertEqual(action.calls, [])
        reasons = [i["reason"] for i in fields(log, "ignored")]
        self.assertIn("below_min_bytes", reasons)

    def poll_cycles(self, f, count_, *, grow_by=0, min_bytes=1_000_000):
        """Run process_file the way the poll backend does: a fresh track each
        cycle, one shared state."""
        state: dict[str, dict] = {}
        action = ScriptedAction()
        with captured() as log, patched(Clock(stop_after=999), OpenScript(0)):
            for cycle in range(count_):
                if grow_by:
                    f.write_bytes(b"x" * (grow_by * (cycle + 1)))
                watch_mod.process_file(
                    f,
                    action,
                    state=state,
                    min_bytes=min_bytes,
                    stable_needed=3,
                    settle_interval=0.1,
                    wait=False,
                )
        return log, state, action

    def test_a_file_parked_below_the_floor_is_mentioned_once_and_then_left(self):
        """The poll backend rescans every cycle and builds a fresh track each
        time, so "already said this" has to live in state instead.

        It did not. The branch that returns for a sub-floor file skipped the
        line at the bottom of the loop that persists the flag, so the file
        announced itself on every scan for as long as the watcher ran — twice
        per scan, in fact, since the branch re-emitted what settle_step had
        just emitted. `progress` then went on doing the same thing on its own,
        which is worse than noise: it reads as work in flight for a file that
        will never be touched.

        copy defaults to --min-bytes 1000000, so any small file left in a
        watched folder hit this, at roughly 34,000 lines a day each.
        """
        f = self.make_file(size=10)

        log, state, action = self.poll_cycles(f, 10)

        self.assertEqual([name for name, _ in log], ["ignored"])
        self.assertTrue(state[str(f)]["said_small"])
        self.assertEqual(state[str(f)]["small_size"], 10)
        self.assertEqual(action.calls, [])

    def test_a_file_growing_towards_the_floor_still_reports_progress(self):
        """Silence is right for a file that is parked, and wrong for one on
        its way up — which is why the size has to be carried across scans as
        well as the flag."""
        f = self.make_file(size=10)

        log, _state, action = self.poll_cycles(f, 5, grow_by=10_000)

        self.assertEqual(count(log, "ignored"), 1)
        self.assertEqual(count(log, "progress"), 4, "went quiet on a live file")
        self.assertEqual(action.calls, [])

    def test_the_event_backend_also_stops_reporting_a_parked_file(self):
        """There the track persists, so nothing needs carrying — but the same
        rule has to hold, or a stalled sub-floor file produces a progress line
        every couple of seconds until the stall timeout fires."""
        f = self.make_file(size=10)
        track: dict = {}

        with captured() as log, patched(None, OpenScript(0)):
            for _ in range(8):
                watch_mod.settle_step(
                    f, track, min_bytes=1_000_000, stable_needed=3
                )

        self.assertEqual([name for name, _ in log], ["ignored"])

    def test_a_file_still_growing_is_left_for_the_next_scan(self):
        """Non-blocking has a budget. A file that outlasts it is not a
        failure, it is simply not finished."""
        f = self.make_file(size=2000)
        action = ScriptedAction()
        clock = Clock(
            stop_after=99,
            on_tick=lambda c: f.write_bytes(b"x" * (2000 + 500 * c.ticks)),
        )

        result, _log = self.process(
            f,
            action,
            clock=clock,
            is_open=OpenScript(0),
            wait=False,
            min_bytes=1000,
            stable_needed=4,
        )

        self.assertIsNone(result)
        self.assertEqual(action.calls, [])

    def test_a_file_that_is_gone_before_the_first_look(self):
        result, _log = self.process(
            self.src / "never-existed.mp4",
            ScriptedAction(),
            clock=Clock(stop_after=50),
            is_open=OpenScript(0),
            wait=False,
        )

        self.assertIsNone(result)

    def test_a_file_that_is_gone_before_the_done_store_check(self):
        store = DoneStore(self.tmp / "done.json")

        result, _log = self.process(
            self.src / "never-existed.mp4",
            ScriptedAction(),
            clock=Clock(stop_after=50),
            is_open=OpenScript(0),
            wait=False,
            done_store=store,
        )

        self.assertIsNone(result)

    def test_a_file_already_handled_is_not_reported_again(self):
        """The poll backend rescans every cycle; re-emitting here grew the log
        without bound. The in-process state answers before the done-store is
        consulted, so a rescan costs neither a log line nor an lsof."""
        f = self.make_file()
        store = DoneStore(self.tmp / "done.json")
        state: dict[str, dict] = {}
        action = ScriptedAction("copied")
        probe = OpenScript(0)

        with captured() as log, patched(Clock(stop_after=99), probe):
            for _ in range(3):
                watch_mod.process_file(
                    f,
                    action,
                    state=state,
                    min_bytes=1,
                    stable_needed=1,
                    settle_interval=0.1,
                    wait=False,
                    done_store=store,
                )

        self.assertEqual(action.calls, [f])
        self.assertEqual(count(log, "copied"), 1)
        self.assertEqual(count(log, "skip"), 0)
        self.assertEqual(len(probe.probes), 1, "paid for lsof on a rescan")

    def test_the_blocking_form_waits_for_the_writer_to_let_go(self):
        f = self.make_file()
        action = ScriptedAction("copied")
        # Held for three looks, then released.
        probe = OpenScript(lambda p: 1 if len(p.probes) <= 3 else 0)

        result, _log = self.process(
            f, action, clock=Clock(stop_after=50), is_open=probe, wait=True
        )

        self.assertEqual(result, "copied")
        self.assertEqual(action.calls, [f])
        self.assertEqual(len(probe.probes), 4)

    def test_the_blocking_form_gives_up_when_the_file_disappears(self):
        f = self.make_file()
        action = ScriptedAction()
        clock = Clock(stop_after=50, on_tick=lambda c: f.unlink(missing_ok=True))

        result, _log = self.process(
            f, action, clock=clock, is_open=OpenScript(1), wait=True
        )

        self.assertIsNone(result)
        self.assertEqual(action.calls, [])


class TestScanning(LoopCase):
    """Listing, baseline seeding and the single-pass scan."""

    def test_nested_files_are_found_only_when_asked_for(self):
        flat = self.make_file()
        sub = self.src / "sub"
        sub.mkdir()
        nested = sub / "deep.mp4"
        nested.write_bytes(b"x" * 100)
        (sub / "notes.txt").write_bytes(b"x" * 100)

        self.assertEqual(watch_mod.iter_watch_files(self.src, ["mp4"]), [flat])
        self.assertEqual(
            sorted(watch_mod.iter_watch_files(self.src, ["mp4"], recursive=True)),
            sorted([flat, nested]),
        )

    def test_a_source_that_is_not_there_lists_nothing_rather_than_raising(self):
        """The loops own announcing this — raising here, or emitting, would
        fire on every scan for as long as the volume stayed away."""
        missing = self.tmp / "gone"

        self.assertEqual(watch_mod.iter_watch_files(missing, None), [])
        self.assertEqual(
            watch_mod.iter_watch_files(missing, None, recursive=True), []
        )

    def test_seeding_records_what_it_skipped_so_a_restart_agrees(self):
        f = self.make_file()
        store = DoneStore(self.tmp / "done.json")

        with captured() as log, patched(Clock(stop_after=9), OpenScript(0)):
            unsettled = watch_mod.seed_baseline(
                self.src,
                ScriptedAction(output=True),
                exts=None,
                min_bytes=1,
                state={},
                done_store=store,
            )

        self.assertEqual(unsettled, [])
        self.assertEqual(len(store), 1)
        self.assertEqual(fields(log, "baseline")[0]["ignored"], 1)
        size, mtime_ns = file_identity(f)
        key = DoneStore.make_key("scripted", f, size=size, mtime_ns=mtime_ns)
        self.assertTrue(store.is_baseline(key))

    def test_a_baseline_skip_with_no_output_is_reported_as_pending(self):
        """A baseline entry is an assumption, not a result. Reporting "skip"
        and exiting 0 claimed total success over untouched work."""
        f = self.make_file()
        store = DoneStore(self.tmp / "done.json")
        size, mtime_ns = file_identity(f)
        store.mark_baseline_file("scripted", f, size=size, mtime_ns=mtime_ns)
        action = ScriptedAction(output=False)

        with captured() as log, patched(Clock(stop_after=9), OpenScript(0)):
            failures = watch_mod.scan_once(
                self.src,
                action,
                exts=None,
                min_bytes=1,
                stable_needed=1,
                settle_interval=0.1,
                state={},
                done_store=store,
            )

        self.assertEqual(failures, 0, "an un-run file is not a failure")
        self.assertEqual(action.calls, [])
        self.assertEqual(fields(log, "pending")[0]["reason"], "baseline_no_output")
        self.assertEqual(fields(log, "baseline_pending")[0]["count"], 1)

    def test_pending_reports_only_files_above_the_floor(self):
        """Strictly read-only — no state, no done-store, not even an lsof — so
        it is safe to point at a folder a watcher is already running on."""
        big = self.make_file("big.mp4", size=5000)
        self.make_file("tiny.mp4", size=10)

        found = watch_mod.list_pending(
            self.src, ScriptedAction(output=False), exts=None, min_bytes=1000
        )

        self.assertEqual(found, [big])


class TestPollBackend(LoopCase):
    """The simple backend. Correct, and the fallback when watchdog is absent."""

    def run_poll(self, action, *, clock, is_open=None, **kw):
        opts = {"exts": None, "poll": 1.0, "stable_needed": 1, "min_bytes": 1}
        opts.update(kw)
        with (
            captured() as log,
            patched(clock, is_open or OpenScript(0)),
            self.assertRaises(StopLoop),
        ):
            watch_mod.run_watch_poll(self.src, action, **opts)
        return log

    def test_the_poll_loop_services_its_retry_queue(self):
        """It used to have none. A copy that failed on an unmounted drive was
        re-attempted on every scan forever, with no backoff and no budget."""
        f = self.make_file()
        retries = RetryQueue(self.tmp / "retry.json")
        self.queue(f, retries)
        action = ScriptedAction("copied")

        log = self.run_poll(
            action, clock=Clock(stop_after=0), retries=retries, catch_up=True
        )

        self.assertEqual(action.calls, [f], "the queued file was not retried")
        self.assertEqual(count(log, "retry"), 1)
        self.assertEqual(len(retries), 0)

    def test_a_queued_file_is_not_claimed_by_the_baseline(self):
        """Seeding marks everything already present as finished. A file the
        queue owns would lose its retry to that."""
        f = self.make_file()
        retries = RetryQueue(self.tmp / "retry.json")
        self.queue(f, retries)

        log = self.run_poll(
            ScriptedAction("copied"), clock=Clock(stop_after=0), retries=retries
        )

        self.assertEqual(fields(log, "baseline")[0]["ignored"], 0)

    def test_a_vanished_source_is_announced_once_and_so_is_its_return(self):
        """Poll recovers by itself, but it used to do so mutely — warning on
        every scan and never saying it had come back."""

        def on_tick(clock):
            if clock.ticks == 1:
                shutil.rmtree(self.src)
            elif clock.ticks == 3:
                self.src.mkdir()

        log = self.run_poll(
            ScriptedAction(), clock=Clock(stop_after=5, on_tick=on_tick)
        )

        self.assertEqual(count(log, "src_lost"), 1)
        self.assertEqual(count(log, "src_restored"), 1)
        self.assertLess(
            kinds(log).index("src_lost"), kinds(log).index("src_restored")
        )

    def test_catch_up_processes_what_is_already_sitting_there(self):
        f = self.make_file()
        action = ScriptedAction("copied")

        log = self.run_poll(action, clock=Clock(stop_after=0), catch_up=True)

        self.assertEqual(action.calls, [f])
        self.assertEqual(count(log, "baseline"), 0)

    def test_without_catch_up_the_back_catalogue_is_left_alone(self):
        """Starting a watcher must never kick off a surprise run over
        everything already in the folder."""
        self.make_file()
        action = ScriptedAction()

        log = self.run_poll(action, clock=Clock(stop_after=0))

        self.assertEqual(action.calls, [])
        self.assertEqual(fields(log, "baseline")[0]["ignored"], 1)


class TestEventHandler(LoopCase):
    """Which filesystem events become work, and which are dropped."""

    def drive(self, action, *, on_tick, ticks=8, is_open=None, **kw):
        """Run the event loop, calling on_tick(clock, handler) each tick."""
        opts = {
            "exts": None,
            "debounce_ms": 50,
            "stable_needed": 1,
            "min_bytes": 1,
            "settle_interval": 0.5,
            "recursive": False,
        }
        opts.update(kw)

        def tick(clock):
            on_tick(clock, FakeObserver.made[-1].handler)

        clock = Clock(stop_after=ticks, on_tick=tick, stop_exc=KeyboardInterrupt)
        with captured() as log, patched(
            clock, is_open or OpenScript(0), fake_observer=True
        ):
            rc = watch_mod.run_watch_event(self.src, action, **opts)
        return rc, log

    def test_a_created_file_is_debounced_then_copied(self):
        f = self.src / "clip.mp4"
        action = ScriptedAction("copied")

        def on_tick(clock, handler):
            if clock.ticks == 1:
                f.write_bytes(b"x" * 100)
                handler.on_created(Event(f))

        rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(rc, 0, "the loop did not exit cleanly")
        self.assertEqual(action.calls, [f])
        self.assertEqual(count(log, "event"), 1)

    def test_a_burst_of_events_for_one_write_is_logged_once(self):
        """FSEvents fires repeatedly for a single write. Debounce already
        coalesces the work; the log should say so too."""
        f = self.src / "clip.mp4"

        def on_tick(clock, handler):
            if clock.ticks == 1:
                f.write_bytes(b"x" * 100)
                for _ in range(5):
                    handler.on_created(Event(f))

        _rc, log = self.drive(ScriptedAction("copied"), on_tick=on_tick)

        self.assertEqual(count(log, "event"), 1)

    def test_directories_and_unmatched_names_are_dropped(self):
        (self.src / "sub").mkdir()
        note = self.src / "notes.txt"
        note.write_bytes(b"x" * 100)

        def on_tick(clock, handler):
            if clock.ticks == 1:
                handler.on_created(Event(self.src / "sub", is_directory=True))
                handler.on_modified(Event(note))

        action = ScriptedAction()
        _rc, log = self.drive(action, on_tick=on_tick, exts=["mp4"])

        self.assertEqual(count(log, "event"), 0)
        self.assertEqual(action.calls, [])

    def test_a_file_outside_the_watched_folder_is_dropped(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        stray = outside / "clip.mp4"
        stray.write_bytes(b"x" * 100)

        def on_tick(clock, handler):
            if clock.ticks == 1:
                handler.on_created(Event(stray))

        action = ScriptedAction()
        _rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(count(log, "event"), 0)
        self.assertEqual(action.calls, [])

    def test_a_nested_file_is_dropped_unless_recursive(self):
        sub = self.src / "sub"
        sub.mkdir()
        nested = sub / "clip.mp4"

        def on_tick(clock, handler):
            if clock.ticks == 1:
                nested.write_bytes(b"x" * 100)
                handler.on_created(Event(nested))

        _rc, flat = self.drive(ScriptedAction(), on_tick=on_tick)
        self.assertEqual(count(flat, "event"), 0)

        action = ScriptedAction("copied")
        _rc, deep = self.drive(action, on_tick=on_tick, recursive=True)
        self.assertEqual(count(deep, "event"), 1)
        self.assertEqual(action.calls, [nested])

    def test_a_symlink_sitting_in_the_folder_is_still_watched(self):
        """Containment is checked against the literal path as well as the
        resolved one. Resolving first sent a symlink off to its target, whose
        parent is elsewhere, and the event was dropped — while the poll backend
        and a single --once pass handled the same file happily."""
        target_dir = self.tmp / "elsewhere"
        target_dir.mkdir()
        target = target_dir / "real.mp4"
        target.write_bytes(b"x" * 100)
        link = self.src / "link.mp4"

        def on_tick(clock, handler):
            if clock.ticks == 1:
                link.symlink_to(target)
                handler.on_created(Event(link))

        action = ScriptedAction("copied")
        _rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(count(log, "event"), 1)
        self.assertEqual(action.calls, [link])

    def test_a_rename_is_read_from_the_destination_path(self):
        f = self.src / "final.mp4"

        def on_tick(clock, handler):
            if clock.ticks == 1:
                f.write_bytes(b"x" * 100)
                handler.on_moved(Event(self.src / "tmp.part", dest=f))

        action = ScriptedAction("copied")
        _rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(fields(log, "event")[0]["name"], "final.mp4")
        self.assertEqual(action.calls, [f])

    def test_a_baseline_file_wakes_the_watcher_only_when_it_changes(self):
        """A re-export can land at the identical size, so mtime has to decide.
        Comparing size alone dropped the event in silence."""
        f = self.make_file(size=100)

        def on_tick(clock, handler):
            if clock.ticks == 1:
                handler.on_modified(Event(f))          # untouched — ignored
            elif clock.ticks == 2:
                stamp = int(f.stat().st_mtime_ns) + 10**9
                os.utime(f, ns=(stamp, stamp))         # same size, new mtime
                handler.on_modified(Event(f))

        action = ScriptedAction("copied")
        _rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(count(log, "event"), 1)
        self.assertEqual(action.calls, [f])

    def test_an_event_arriving_by_an_outside_path_is_still_ours(self):
        """A link elsewhere pointing into the folder resolves to a file we do
        watch, so the event counts — but it has no path relative to the folder
        to log, and asking for one used to be the end of the loop."""
        alias_dir = self.tmp / "elsewhere"
        alias_dir.mkdir()
        real = self.src / "real.mp4"
        alias = alias_dir / "alias.mp4"

        def on_tick(clock, handler):
            if clock.ticks == 1:
                real.write_bytes(b"x" * 100)
                alias.symlink_to(real)
                handler.on_created(Event(alias))

        action = ScriptedAction("copied")
        _rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(fields(log, "event")[0]["path"], "alias.mp4")
        self.assertEqual(action.calls, [alias])

    def test_a_baseline_file_deleted_before_it_can_be_checked_is_dropped(self):
        f = self.make_file()

        def on_tick(clock, handler):
            if clock.ticks == 1:
                f.unlink()
                handler.on_modified(Event(f))

        action = ScriptedAction()
        _rc, log = self.drive(action, on_tick=on_tick)

        self.assertEqual(count(log, "event"), 0)
        self.assertEqual(action.calls, [])

    def test_fresh_content_supersedes_a_queued_retry(self):
        """The queue holds the bytes that failed; a new write invalidates it."""
        f = self.make_file()
        retries = RetryQueue(self.tmp / "retry.json")
        self.queue(f, retries, due=False)

        def on_tick(clock, handler):
            if clock.ticks == 1:
                handler.on_modified(Event(f))

        self.drive(ScriptedAction("copied"), on_tick=on_tick, retries=retries)

        self.assertEqual(len(retries), 0)


class TestEventStallPaths(LoopCase):
    """What the loop does with a file that stops moving.

    The first version of this dropped a stalled file outright. That abandoned
    finished work: releasing a write handle does not reliably emit an FSEvent,
    so nothing would ever pick the file up again.
    """

    def drive(self, action, *, on_tick, ticks, is_open, **kw):
        opts = {
            "exts": None,
            "debounce_ms": 50,
            "stable_needed": 1,
            "min_bytes": 1,
            "settle_interval": 0.5,
            "recursive": False,
        }
        opts.update(kw)
        clock = Clock(stop_after=ticks, on_tick=on_tick, stop_exc=KeyboardInterrupt)
        with captured() as log, patched(clock, is_open, fake_observer=True):
            watch_mod.run_watch_event(self.src, action, **opts)
        return log

    @staticmethod
    def _skip_ahead(probe, *steps):
        """Jump the clock the moment the loop has actually looked at the file.

        Counting ticks instead would be guesswork: how many the debounce eats
        is an artefact of floating-point subtraction, not of the behaviour
        under test. Every probe is one look — the first is baseline seeding.
        """
        done: list[float] = []

        def on_tick(clock):
            for after, seconds in steps:
                if len(probe.probes) >= after and seconds not in done:
                    done.append(seconds)
                    clock.jump(seconds)
                    return

        return on_tick

    def test_a_writer_that_still_holds_the_file_is_re_checked_slowly(self):
        """Held open is not the same as stuck. Forgetting it here is what
        abandoned finished work."""
        self.make_file()
        probe = OpenScript(1)
        action = ScriptedAction()

        log = self.drive(
            action,
            on_tick=self._skip_ahead(probe, (2, watch_mod.STALL_TIMEOUT + 100)),
            ticks=40,
            is_open=probe,
        )

        stalled = fields(log, "stalled")
        self.assertEqual(len(stalled), 1, "said it repeatedly, or not at all")
        self.assertGreater(stalled[0]["stalled_s"], watch_mod.STALL_TIMEOUT)
        self.assertEqual(count(log, "give_up"), 0)
        self.assertEqual(action.calls, [])

    def test_a_file_held_open_for_a_full_day_is_finally_abandoned(self):
        self.make_file()
        probe = OpenScript(1)

        log = self.drive(
            ScriptedAction(),
            # Two probes in, cross the stall threshold; two probes after that
            # (the slow re-check and its lsof), cross the abandon threshold.
            on_tick=self._skip_ahead(
                probe,
                (2, watch_mod.STALL_TIMEOUT + 100),
                (4, watch_mod.STALL_ABANDON_AFTER + 100),
            ),
            ticks=40,
            is_open=probe,
        )

        gave_up = fields(log, "give_up")
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(gave_up[0]["reason"], "held_open_too_long")
        self.assertEqual(count(log, "stalled"), 1)

    def test_a_writer_that_let_go_without_an_event_is_still_picked_up(self):
        """Releasing a handle emits no FSEvent. The stall check asks lsof
        directly rather than inferring an answer from the silence."""
        f = self.make_file()
        # The first probe at any given moment is the settle gate, which still
        # finds the writer in place. A second probe at that same moment can
        # only be the stall check — by which point the writer has gone.
        probe = OpenScript(lambda p: 0 if p.probes.count(p.probes[-1]) > 1 else 1)
        action = ScriptedAction("copied")

        log = self.drive(
            action,
            on_tick=self._skip_ahead(probe, (2, watch_mod.STALL_TIMEOUT + 100)),
            ticks=40,
            is_open=probe,
        )

        late = fields(log, "settled_late")
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["name"], f.name)
        self.assertEqual(action.calls, [f])
        self.assertEqual(count(log, "give_up"), 0)

    def test_a_file_deleted_while_being_watched_is_forgotten(self):
        f = self.make_file()

        def on_tick(clock):
            if clock.ticks == 2:
                f.unlink()

        action = ScriptedAction()
        log = self.drive(action, on_tick=on_tick, ticks=10, is_open=OpenScript(1))

        self.assertEqual(action.calls, [])
        self.assertEqual(count(log, "give_up"), 0)
        self.assertEqual(count(log, "stalled"), 0)


class TestEventSourceLoss(LoopCase):
    """FSEvents goes quiet when the directory is deleted or its volume
    unmounts. Without a check the process stays alive and healthy forever,
    watching nothing and logging nothing."""

    def test_the_watcher_waits_for_the_source_and_catches_up_on_return(self):
        gone_at: list[int] = []

        def on_tick(clock):
            if clock.ticks == 1:
                shutil.rmtree(self.src)
                clock.jump(watch_mod.SRC_CHECK_INTERVAL + 1)
                return
            if not self.src.is_dir():
                gone_at.append(clock.ticks)
                # Twelve waits of SRC_WAIT_INTERVAL is a full log interval.
                if len(gone_at) > 12:
                    self.src.mkdir()
                    (self.src / "landed-while-away.mp4").write_bytes(b"x" * 100)

        action = ScriptedAction("copied")
        clock = Clock(stop_after=30, on_tick=on_tick, stop_exc=KeyboardInterrupt)
        with captured() as log, patched(clock, OpenScript(0), fake_observer=True):
            watch_mod.run_watch_event(
                self.src,
                action,
                exts=None,
                debounce_ms=50,
                stable_needed=1,
                min_bytes=1,
                settle_interval=0.5,
                recursive=False,
            )

        self.assertEqual(count(log, "src_lost"), 1)
        self.assertEqual(count(log, "src_restored"), 1)
        self.assertGreaterEqual(count(log, "src_waiting"), 1)
        self.assertEqual(
            len(FakeObserver.made), 2, "the observer was not restarted"
        )
        self.assertTrue(FakeObserver.made[0].stopped)
        self.assertEqual(
            [p.name for p in action.calls],
            ["landed-while-away.mp4"],
            "work that arrived during the outage was treated as old news",
        )


class TestEventRetryServicing(LoopCase):
    """The event backend's only source of new work is an FSEvent on the
    source. A destination coming back produces none, so the queue is the
    watcher's only way to notice."""

    def drive(self, action, retries, *, on_tick=None, ticks=20, **kw):
        opts = {
            "exts": None,
            "debounce_ms": 50,
            "stable_needed": 1,
            "min_bytes": 1,
            "settle_interval": 0.5,
            "recursive": False,
            "retries": retries,
        }
        opts.update(kw)
        clock = Clock(
            stop_after=ticks,
            on_tick=on_tick or (lambda c: None),
            stop_exc=KeyboardInterrupt,
        )
        with captured() as log, patched(clock, OpenScript(0), fake_observer=True):
            watch_mod.run_watch_event(self.src, action, **opts)
        return log

    def test_a_due_retry_runs_without_a_new_event(self):
        f = self.make_file()
        retries = RetryQueue(self.tmp / "retry.json")
        self.queue(f, retries)
        action = ScriptedAction("copied")

        log = self.drive(
            action,
            retries,
            on_tick=lambda c: c.jump(watch_mod.RETRY_SCAN_INTERVAL + 1)
            if c.ticks == 1
            else None,
        )

        self.assertEqual(action.calls, [f])
        self.assertEqual(count(log, "retry"), 1)
        self.assertEqual(len(retries), 0)

    def test_a_retry_whose_file_changed_is_pushed_back_into_settle(self):
        """It settled once, which is why the action ran. New bytes have to
        settle again before anything copies them."""
        f = self.make_file()
        retries = RetryQueue(self.tmp / "retry.json")
        self.queue(f, retries)
        f.write_bytes(b"y" * 5000)
        action = ScriptedAction("copied")

        log = self.drive(
            action,
            retries,
            on_tick=lambda c: c.jump(watch_mod.RETRY_SCAN_INTERVAL + 1)
            if c.ticks == 1
            else None,
        )

        self.assertEqual(count(log, "retry"), 0, "stale bytes were retried")
        self.assertEqual(action.calls, [f], "the rewrite was never picked up")
        self.assertEqual(len(retries), 0)


class TestEventCatchUp(LoopCase):
    def test_catch_up_hands_an_unfinished_file_to_the_settle_loop(self):
        """The catch-up scan never blocks, so a file still being written falls
        through it. It has to be handed on, not dropped — no further FSEvent
        is guaranteed once the writer lets go."""
        f = self.make_file()
        # Open for the catch-up scan's look, closed by the time the loop asks.
        probe = OpenScript(lambda p: 1 if len(p.probes) <= 1 else 0)
        action = ScriptedAction("copied")
        clock = Clock(stop_after=10, stop_exc=KeyboardInterrupt)

        with captured() as log, patched(clock, probe, fake_observer=True):
            watch_mod.run_watch_event(
                self.src,
                action,
                exts=None,
                debounce_ms=50,
                stable_needed=1,
                min_bytes=1,
                settle_interval=0.5,
                recursive=False,
                catch_up=True,
            )

        self.assertEqual(count(log, "baseline"), 0)
        self.assertEqual(action.calls, [f], "the unfinished file was dropped")


class TestBackendSelection(LoopCase):
    def test_a_missing_watchdog_falls_back_to_polling(self):
        f = self.make_file()
        action = ScriptedAction("copied")
        clock = Clock(stop_after=0)
        blocked = {"watchdog.observers": None}
        saved = {k: sys.modules.get(k) for k in blocked}
        sys.modules.update(blocked)
        try:
            with (
                captured() as log,
                patched(clock, OpenScript(0)),
                contextlib.redirect_stderr(io.StringIO()) as err,
                self.assertRaises(StopLoop),
            ):
                watch_mod.run_watch_event(
                    self.src,
                    action,
                    exts=None,
                    debounce_ms=50,
                    stable_needed=1,
                    min_bytes=1,
                    settle_interval=0.5,
                    recursive=False,
                    catch_up=True,
                )
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

        self.assertIn("watchdog not installed", err.getvalue())
        self.assertIn("--backend poll", err.getvalue())
        self.assertEqual(action.calls, [f], "the fallback never ran")
        self.assertEqual(count(log, "start"), 0)


class TestRunWatchLifecycle(LoopCase):
    """Pidfiles, duplicate watchers and signal handling around the loops."""

    def setUp(self):
        super().setUp()
        from folder_watch.support.paths import PID_DIR

        self.pid_dir = PID_DIR
        self.pid_dir.mkdir(parents=True, exist_ok=True)
        self.planted: list[Path] = []

    def tearDown(self):
        for path in self.planted:
            path.unlink(missing_ok=True)
        for path in self.pid_dir.glob("scripted-src.*.json"):
            path.unlink(missing_ok=True)
        super().tearDown()

    def plant_foreign_pidfile(self, name: str) -> Path:
        """A pidfile for another live process, which is what a duplicate
        watcher looks like from here."""
        import json

        from folder_watch.support.pidfile import pidfile_path

        other = os.getppid()
        path = pidfile_path(name, pid=other)
        path.write_text(json.dumps({"pid": other, "name": name}) + "\n")
        self.planted.append(path)
        return path

    def our_pidfiles(self, name: str) -> list[Path]:
        from folder_watch.support.pidfile import pidfile_path, safe_name

        mine = pidfile_path(name)
        return [
            p
            for p in self.pid_dir.glob(f"{safe_name(name)}.*.json")
            if p == mine
        ]

    def run_watch(self, action, *, clock=None, src=None, observer=False, **kw):
        opts = {
            "exts": None,
            "poll": 1.0,
            "stable_needed": 1,
            "min_bytes": 1,
            "once": False,
            "backend": "poll",
            "catch_up": True,
        }
        opts.update(kw)
        rc = None
        with (
            captured() as log,
            patched(clock, OpenScript(0), fake_observer=observer),
            # A clock means the loop has no exit of its own; it stops by
            # raising, after the finally that clears the pidfile has run.
            contextlib.suppress(StopLoop),
        ):
            rc = watch_mod.run_watch(src or self.src, action, **opts)
        return rc, log

    def test_a_missing_source_is_an_error_not_an_empty_folder(self):
        """Reporting "done failed=0" made a typo and an unmounted volume
        indistinguishable from a folder with nothing in it."""
        rc, log = self.run_watch(
            ScriptedAction(), once=True, src=self.tmp / "not-here"
        )

        self.assertEqual(rc, 2)
        self.assertEqual(fields(log, "error")[0]["reason"], "src_missing")

    def test_exclusive_refuses_to_start_beside_another_watcher(self):
        self.plant_foreign_pidfile("scripted-src")

        rc, log = self.run_watch(ScriptedAction(), exclusive=True)

        self.assertEqual(rc, 2)
        self.assertEqual(fields(log, "error")[0]["reason"], "already_running")

    def test_a_duplicate_watcher_warns_but_still_starts(self):
        """Under launchd KeepAlive a watcher that refused to start would flap
        instead of running, which for unattended work is worse."""
        self.plant_foreign_pidfile("scripted-src")

        _rc, log = self.run_watch(ScriptedAction(), clock=Clock(stop_after=0))

        warns = [w["reason"] for w in fields(log, "warn")]
        self.assertIn("duplicate_watcher", warns)
        self.assertEqual(count(log, "start"), 1)

    def test_a_dry_run_writes_no_pidfile(self):
        """A pidfile is a write, and it also made a preview run collide with
        an exclusive one."""
        seen: list[int] = []
        clock = Clock(
            stop_after=1,
            on_tick=lambda c: seen.append(len(self.our_pidfiles("scripted-src"))),
        )

        self.run_watch(ScriptedAction(), clock=clock, dry_run=True)

        self.assertEqual(seen, [0])

    def test_the_pidfile_lives_exactly_as_long_as_the_watch(self):
        seen: list[int] = []
        clock = Clock(
            stop_after=1,
            on_tick=lambda c: seen.append(len(self.our_pidfiles("scripted-src"))),
        )

        self.run_watch(ScriptedAction(), clock=clock)

        self.assertEqual(seen, [1], "no pidfile while the watch was running")
        self.assertEqual(self.our_pidfiles("scripted-src"), [])

    def test_the_previous_sigterm_handler_is_put_back(self):
        """The handler is installed so launchd's stop unwinds instead of
        killing the process outright — but it belongs to the watch, not to
        whatever runs next."""
        import signal

        before = signal.getsignal(signal.SIGTERM)

        self.run_watch(ScriptedAction(), clock=Clock(stop_after=0))

        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_catch_up_clears_the_assumptions_baseline_made(self):
        store = DoneStore(self.tmp / "done.json")
        f = self.make_file()
        size, mtime_ns = file_identity(f)
        store.mark_baseline_file("scripted", f, size=size, mtime_ns=mtime_ns)
        self.assertEqual(len(store), 1)

        _rc, log = self.run_watch(
            ScriptedAction("copied"),
            clock=Clock(stop_after=0),
            done_store=store,
            catch_up=True,
        )

        self.assertEqual(fields(log, "catch_up")[0]["cleared_baseline"], 1)
        self.assertEqual(fields(log, "start")[0]["catch_up"], True)

    def test_a_single_pass_reports_failures_through_its_exit_status(self):
        """--once used to return 0 even when every copy failed, so cron and CI
        could not tell."""
        self.make_file()

        rc, log = self.run_watch(ScriptedAction("fail"), once=True)

        self.assertEqual(rc, 1)
        self.assertEqual(fields(log, "done")[0]["failed"], 1)
        self.assertEqual(fields(log, "start")[0]["catch_up"], "n/a")

    def test_an_empty_folder_is_not_a_failure(self):
        rc, log = self.run_watch(ScriptedAction(), once=True)

        self.assertEqual(rc, 0)
        self.assertEqual(fields(log, "done")[0]["failed"], 0)

    def test_a_stop_signal_unwinds_so_the_pidfile_goes_with_it(self):
        """launchd stops services with SIGTERM, whose default disposition kills
        the process outright — the finally never runs and the pidfile is left
        behind looking stale."""
        import signal

        def on_tick(clock):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with self.assertRaises(SystemExit):
            self.run_watch(
                ScriptedAction(), clock=Clock(stop_after=5, on_tick=on_tick)
            )

        self.assertEqual(self.our_pidfiles("scripted-src"), [])

    def test_a_watch_started_off_the_main_thread_still_runs(self):
        """Only the main thread may install a signal handler. Embedding the
        watch anywhere else must degrade to "no handler", not to a crash."""
        import threading

        f = self.make_file()
        action = ScriptedAction("copied")
        outcome: list[object] = []

        def body():
            try:
                self.run_watch(action, clock=Clock(stop_after=0))
            except BaseException as exc:
                # Reported, not swallowed — a thread's traceback would
                # otherwise go to stderr and the test would still pass.
                outcome.append(exc)

        thread = threading.Thread(target=body)
        thread.start()
        thread.join(timeout=5)

        self.assertEqual(outcome, [])
        self.assertEqual(action.calls, [f])

    def test_the_event_backend_is_what_a_plain_watch_gets(self):
        f = self.make_file()
        action = ScriptedAction("copied")
        clock = Clock(stop_after=6, stop_exc=KeyboardInterrupt)

        rc, log = self.run_watch(
            action, clock=clock, backend="event", observer=True, catch_up=True
        )

        self.assertEqual(rc, 0)
        self.assertEqual(fields(log, "start")[0]["backend"], "event")
        self.assertEqual(len(FakeObserver.made), 1)
        self.assertEqual(action.calls, [f])


if __name__ == "__main__":
    unittest.main()
