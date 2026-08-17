#!/usr/bin/env python3
"""Tests for the code that physically moves files.

atomic_copy's happy path was well covered and its failure paths were not
covered at all — the retry loop, the escape for errors that will never clear,
the temp-file cleanup and the final re-raise. For code whose job is moving
someone's only copy of a file, the failure paths are the ones that matter:
a success that works is unremarkable, a failure that leaves a half-written
file at the destination is a lost render.

Two properties carry the weight:

* the destination is never partially written, whatever happens mid-copy;
* a failed attempt leaves no temp file behind, so a flaky drive cannot
  silently accumulate full-size hidden files next to the real ones.
"""
from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)
from _loopharness import captured, count, fields

import folder_watch.support.copy_io as copy_io
from folder_watch.support.copy_io import atomic_copy, sweep_stale_temps


class FakeClock:
    """Records sleeps instead of taking them, so backoff is assertable."""

    def __init__(self):
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    @staticmethod
    def time() -> float:
        import time

        return time.time()


class ShutilShim:
    """Stands in for copy_io's `shutil`, which is all it uses of it.

    Assigning to `copy_io.shutil.copy2` would reach into the real shutil
    module — shared by the whole process, including the test's own fixtures —
    so a stand-in that delegates would call itself. Rebinding the name inside
    copy_io swaps only what copy_io sees.
    """

    def __init__(self, copy2):
        self.copy2 = copy2


class Failing:
    """A copy2 that fails a set number of times, then works.

    `error` is raised for the first `times` calls. Anything after that is
    delegated to the real copy2 — captured at construction, not looked up —
    so the success case is a genuine copy.
    """

    def __init__(self, error: OSError, times: int):
        self.error = error
        self.times = times
        self.calls = 0
        self.real = shutil.copy2

    def __call__(self, src, dst, *a, **kw):
        self.calls += 1
        if self.calls <= self.times:
            raise self.error
        return self.real(src, dst, *a, **kw)


def oserror(code: int) -> OSError:
    return OSError(code, os.strerror(code))


class CopyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.dest_dir = self.tmp / "dest"
        self.dest_dir.mkdir()
        self.src = self.tmp / "clip.mp4"
        self.src.write_bytes(b"payload" * 1000)
        self.dest = self.dest_dir / "clip.mp4"
        self.clock = FakeClock()
        self._real_time = copy_io.time
        self._real_shutil = copy_io.shutil
        copy_io.time = self.clock

    def tearDown(self):
        copy_io.time = self._real_time
        copy_io.shutil = self._real_shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def temps_left(self) -> list[Path]:
        return list(self.dest_dir.glob(".*.tmp"))


class TestRetries(CopyCase):
    def test_a_transient_failure_is_retried_and_then_succeeds(self):
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=2))

        with captured() as log:
            atomic_copy(self.src, self.dest, retries=5, backoff=0.5)

        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())
        self.assertEqual(count(log, "copy_retry"), 2)
        self.assertEqual([f["attempt"] for f in fields(log, "copy_retry")], [1, 2])

    def test_the_backoff_grows_with_each_attempt(self):
        """A drive that has gone away needs longer than a blip, and a flat
        retry either gives up too early or hammers it."""
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=3))

        with captured():
            atomic_copy(self.src, self.dest, retries=5, backoff=0.5)

        self.assertEqual(self.clock.slept, [0.5, 1.0, 1.5])

    def test_running_out_of_retries_raises_the_last_error(self):
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=99))

        with captured() as log, self.assertRaises(OSError) as caught:
            atomic_copy(self.src, self.dest, retries=3, backoff=0.1)

        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(count(log, "copy_retry"), 3, "wrong number of attempts")

    def test_a_permanent_error_is_not_retried_at_all(self):
        """Read-only or forbidden will not become writable by waiting, and
        retrying it burns seconds per file across a whole backlog."""
        for code in (errno.EACCES, errno.EPERM, errno.EROFS):
            with self.subTest(errno=errno.errorcode[code]):
                failing = Failing(oserror(code), times=99)
                copy_io.shutil = ShutilShim(failing)
                self.clock.slept.clear()

                with captured() as log, self.assertRaises(OSError):
                    atomic_copy(self.src, self.dest, retries=5, backoff=0.1)

                self.assertEqual(failing.calls, 1, "it retried a certainty")
                self.assertEqual(count(log, "copy_retry"), 0)
                self.assertEqual(self.clock.slept, [], "it slept before giving up")


class TestNothingIsLeftBehind(CopyCase):
    def test_a_failed_attempt_leaves_no_temp_file(self):
        """A flaky destination would otherwise accumulate full-size hidden
        files beside the real ones, which is what the stale sweep exists to
        clean up after — better not to create them."""
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=99))

        with captured(), self.assertRaises(OSError):
            atomic_copy(self.src, self.dest, retries=3, backoff=0.1)

        self.assertEqual(self.temps_left(), [])

    def test_a_permanent_failure_leaves_no_temp_file(self):
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EACCES), times=99))

        with captured(), self.assertRaises(OSError):
            atomic_copy(self.src, self.dest, retries=3, backoff=0.1)

        self.assertEqual(self.temps_left(), [])

    def test_a_successful_copy_leaves_no_temp_file(self):
        with captured():
            atomic_copy(self.src, self.dest)

        self.assertEqual(self.temps_left(), [])

    def test_temps_are_cleaned_up_even_after_several_failed_attempts(self):
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=2))

        with captured():
            atomic_copy(self.src, self.dest, retries=5, backoff=0.1)

        self.assertEqual(self.temps_left(), [])
        self.assertTrue(self.dest.is_file())


class TestTheDestinationIsNeverHalfWritten(CopyCase):
    """The reason this is not just shutil.copy2."""

    def test_a_failed_copy_never_creates_the_destination(self):
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=99))

        with captured(), self.assertRaises(OSError):
            atomic_copy(self.src, self.dest, retries=2, backoff=0.1)

        self.assertFalse(
            self.dest.exists(), "a partial file was left where a copy should be"
        )

    def test_a_failed_copy_does_not_damage_an_existing_destination(self):
        """The dangerous case: overwriting a good file with a failed copy.

        The previous version has to survive intact, because at this point it
        is the only version that is known to be complete.
        """
        self.dest.write_bytes(b"the previous good version")
        copy_io.shutil = ShutilShim(Failing(oserror(errno.EIO), times=99))

        with captured(), self.assertRaises(OSError):
            atomic_copy(self.src, self.dest, retries=2, backoff=0.1)

        self.assertEqual(self.dest.read_bytes(), b"the previous good version")

    def test_the_destination_appears_complete_or_not_at_all(self):
        """os.replace is atomic, so a reader never sees a growing file.

        Checked by watching what exists at the moment the copy is interrupted:
        the temp is a hidden name, and the real name only appears once the
        bytes are all there.
        """
        seen: list[bool] = []
        real_copy2 = shutil.copy2

        def watching_copy2(s, d, *a, **kw):
            # Mid-copy: the temp exists, the destination must not.
            seen.append(self.dest.exists())
            return real_copy2(s, d, *a, **kw)

        copy_io.shutil = ShutilShim(watching_copy2)

        with captured():
            atomic_copy(self.src, self.dest)

        self.assertEqual(seen, [False], "the destination existed mid-copy")
        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())


class TestStaleTempSweep(CopyCase):
    def test_an_old_leftover_is_removed_and_announced(self):
        stale = self.dest_dir / f".{self.dest.name}.abc.tmp"
        stale.write_bytes(b"interrupted")
        old = self._real_time.time() - 7200
        os.utime(stale, (old, old))

        with captured() as log:
            removed = sweep_stale_temps(self.dest)

        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertEqual(fields(log, "cleanup")[0]["reason"], "stale_temp")

    def test_a_copy_in_flight_is_left_alone(self):
        """The age threshold is the whole safety margin: sweeping a temp that
        another watcher is still writing would destroy a live copy."""
        fresh = self.dest_dir / f".{self.dest.name}.xyz.tmp"
        fresh.write_bytes(b"still being written")

        with captured() as log:
            removed = sweep_stale_temps(self.dest)

        self.assertEqual(removed, 0)
        self.assertTrue(fresh.is_file())
        self.assertEqual(count(log, "cleanup"), 0)

    def test_only_this_destination_s_temps_are_considered(self):
        other = self.dest_dir / ".something-else.abc.tmp"
        other.write_bytes(b"not ours")
        old = self._real_time.time() - 7200
        os.utime(other, (old, old))

        with captured():
            removed = sweep_stale_temps(self.dest)

        self.assertEqual(removed, 0)
        self.assertTrue(other.is_file(), "swept a temp belonging to another copy")

    def test_an_unreadable_destination_directory_is_not_an_error(self):
        """Called on every copy, including the first one to an unmounted
        volume. Raising here would turn a retryable copy into a crash."""
        with captured():
            removed = sweep_stale_temps(self.tmp / "not-there" / "file.mp4")

        self.assertEqual(removed, 0)

    def test_a_leftover_that_vanishes_mid_sweep_is_skipped(self):
        """Two watchers on one destination will race here. Losing the race is
        the normal outcome, not a failure."""
        ghost = self.dest_dir / f".{self.dest.name}.gone.tmp"
        ghost.write_bytes(b"x")
        old = self._real_time.time() - 7200
        os.utime(ghost, (old, old))

        real_glob = Path.glob

        def glob_then_delete(self_path, pattern):
            found = list(real_glob(self_path, pattern))
            for p in found:
                p.unlink(missing_ok=True)  # the other watcher got there first
            return iter(found)

        Path.glob = glob_then_delete
        try:
            with captured():
                removed = sweep_stale_temps(self.dest)
        finally:
            Path.glob = real_glob

        self.assertEqual(removed, 0)


if __name__ == "__main__":
    unittest.main()


class TestTheSweepFindsDebrisFromOtherFiles(CopyCase):
    """The sweep used to be scoped to the filename being copied.

    So a copy of render-042.mp4 killed mid-flight left a full-size hidden temp
    that only a *second copy of render-042.mp4* would ever clean up. Render
    names are unique per export, so in practice nothing ever cleaned it: the
    destination accumulated hidden files at full size, invisible in Finder,
    untouched by prune. Reported at 6.0 G of debris for 3.0 G of source.
    """

    def stale(self, name: str) -> Path:
        leftover = self.dest_dir / name
        leftover.write_bytes(b"abandoned" * 1000)
        old = self._real_time.time() - 7200
        os.utime(leftover, (old, old))
        return leftover

    def test_debris_from_a_different_file_is_swept(self):
        orphan = self.stale(f"{copy_io.TMP_MARKER}xyz.tmp")

        with captured() as log:
            atomic_copy(self.src, self.dest)

        self.assertFalse(orphan.exists(), "debris from another copy was left behind")
        self.assertEqual(fields(log, "cleanup")[0]["reason"], "stale_temp")

    def test_debris_named_the_old_way_is_still_swept(self):
        """Anyone upgrading has leftovers under the previous naming."""
        legacy = self.stale(f".{self.dest.name}.abc.tmp")

        with captured():
            atomic_copy(self.src, self.dest)

        self.assertFalse(legacy.exists())

    def test_a_sibling_copy_still_in_flight_is_spared(self):
        """Age is the only signal that separates abandoned debris from a temp
        another watcher is actively writing, so it has to stay decisive."""
        fresh = self.dest_dir / f"{copy_io.TMP_MARKER}live.tmp"
        fresh.write_bytes(b"being written right now")

        with captured():
            atomic_copy(self.src, self.dest)

        self.assertTrue(fresh.is_file(), "a live copy's temp was deleted")

    def test_it_never_removes_a_file_it_did_not_write(self):
        """The marker is what makes the sweep safe to widen. A hidden .tmp that
        belongs to somebody else must survive, however old it is."""
        theirs = self.dest_dir / ".editor-autosave.tmp"
        theirs.write_bytes(b"someone else's file")
        old = self._real_time.time() - 999999
        os.utime(theirs, (old, old))

        with captured():
            atomic_copy(self.src, self.dest)

        self.assertTrue(theirs.is_file(), "swept a file the tool never wrote")
        self.assertEqual(theirs.read_bytes(), b"someone else's file")

    def test_the_temp_no_longer_carries_the_filename(self):
        """Which also fixes a long-name problem: prefixing a 200-character
        export name onto the temp could exceed the filesystem limit."""
        long_name = "x" * 200 + ".mp4"
        src = self.tmp / long_name
        src.write_bytes(b"payload")
        seen: list[str] = []

        real = shutil.copy2

        def watching(s, d, *a, **kw):
            seen.append(Path(d).name)
            return real(s, d, *a, **kw)

        copy_io.shutil = ShutilShim(watching)
        with captured():
            atomic_copy(src, self.dest_dir / long_name)

        self.assertTrue(seen and seen[0].startswith(copy_io.TMP_MARKER))
        self.assertNotIn("x" * 200, seen[0])
