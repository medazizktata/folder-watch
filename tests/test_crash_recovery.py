#!/usr/bin/env python3
"""What survives the process being killed mid-operation.

The last item on the original improvement list, and the only one left where
being wrong loses data: "kill mid-copy / mid-unzip; assert no half files /
zip kept".

Everything else in the suite tests a failure the code got to handle — an
exception raised, an error code returned, a cleanup branch entered. SIGKILL is
different in kind: no handler runs, no `finally`, no temp is tidied. The
invariants have to hold because of how the work is ordered on disk, not
because of anything the program does on the way down. That is the case a
closing laptop lid, a `launchctl kill -9` and a panic all reduce to.

Each test starts a real watcher, polls for hard evidence the operation is in
flight, and kills it at that moment. Nothing here sleeps and hopes.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

# Big enough that mkstemp-to-rename is a window a tight poll always catches,
# small enough to stay cheap in the commit gate. Measured, not guessed.
PAYLOAD_MB = 8
DEADLINE = 30.0


class CrashCase(unittest.TestCase):
    """Every test gets its own throwaway directory tree.

    These start real watchers and kill them mid-write, so they leave temps and
    half-finished work behind by design. That has to happen somewhere nobody
    cares about — never a real folder, and never the shared state directory.
    Asserted in setUp rather than assumed, because the cost of being wrong here
    is somebody's project.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()
        self.proc: subprocess.Popen | None = None
        self.assert_isolated()

    def assert_isolated(self) -> None:
        scratch = Path(tempfile.gettempdir()).resolve()
        home = Path.home().resolve()
        for label, path in (("work dir", self.tmp), ("source", self.src)):
            self.assertTrue(
                str(path).startswith(str(scratch)),
                f"{label} {path} is not under {scratch}",
            )
            self.assertFalse(
                str(path).startswith(str(home / "Desktop")),
                f"{label} {path} is inside a real folder",
            )
        # The child inherits these; if they still point at the real ones, a
        # killed watcher would leave debris in the state the live ones use.
        for var in ("FOLDER_WATCH_STATE_DIR", "FOLDER_WATCH_CONFIG_DIR"):
            value = os.environ.get(var, "")
            self.assertTrue(value, f"{var} is unset — child would use the real dir")
            self.assertIn("folder-watch-tests", value, f"{var} is not redirected")

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, *argv) -> subprocess.Popen:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "folder_watch", *argv, "--no-notify"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
        )
        return self.proc

    def kill_when(self, condition) -> bool:
        """SIGKILL the child the instant `condition()` is true.

        Returns whether it was caught in flight. Polled rather than timed:
        the point is to kill *during* the operation, and a sleep long enough
        to be sure on one machine is too long on another.
        """
        assert self.proc is not None
        deadline = time.time() + DEADLINE
        while time.time() < deadline:
            if condition():
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait(timeout=10)
                return True
            if self.proc.poll() is not None:
                return False
        return False

    def make_payload(self, name="big.mp4") -> Path:
        f = self.src / name
        f.write_bytes(os.urandom(1 << 20) * PAYLOAD_MB)
        return f

    def temps(self) -> list[Path]:
        return list(self.dest.glob(".*.tmp"))

    def copy_in_flight(self) -> bool:
        """Bytes are actually moving, not merely that a temp name exists.

        Killing the moment mkstemp returns lands before the data phase, so a
        copy that wrote straight to the destination would still pass. Waiting
        for the temp to have content puts the kill where it can tell the two
        apart.
        """
        for t in self.temps():
            try:
                if t.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False


class TestKilledMidCopy(CrashCase):
    def copy_until_temp_appears(self) -> bool:
        self.start(
            "copy", str(self.src), "--to", str(self.dest),
            "--once", "--catch-up", "--no-done-store", "--min-bytes", "1",
        )
        return self.kill_when(self.copy_in_flight)

    def test_the_destination_is_never_left_half_written(self):
        """The whole reason for temp-then-rename. os.replace is atomic, so the
        real name either does not exist or holds every byte — there is no
        moment where a reader, or the next scan, can see a partial file under
        the name that means "this one is finished"."""
        src = self.make_payload()

        self.assertTrue(self.copy_until_temp_appears(), "never caught it copying")

        landed = self.dest / src.name
        self.assertFalse(landed.exists(), "a partial file is sitting at the destination")

    def test_the_source_is_untouched(self):
        """copy never deletes, and a crash must not make it start."""
        src = self.make_payload()
        before = src.read_bytes()

        self.copy_until_temp_appears()

        self.assertTrue(src.is_file())
        self.assertEqual(src.read_bytes(), before)

    def test_the_leftover_is_a_temp_that_a_later_run_sweeps(self):
        """Nothing can tidy up after SIGKILL, so the leftover is expected —
        what matters is that it is parked under a hidden temp name rather than
        the real one, and that the next copy to this destination clears it."""
        src = self.make_payload()
        self.assertTrue(self.copy_until_temp_appears())

        leftover = self.temps()
        self.assertEqual(len(leftover), 1, "expected exactly one temp to remain")
        self.assertTrue(leftover[0].name.startswith("."), "leftover is not hidden")

        # Age it past the sweep threshold; a fresh temp is deliberately spared,
        # because it could belong to a copy still running.
        old = time.time() - 7200
        os.utime(leftover[0], (old, old))

        done = subprocess.run(
            [sys.executable, "-m", "folder_watch", "copy", str(self.src),
             "--to", str(self.dest), "--once", "--catch-up", "--no-done-store",
             "--min-bytes", "1", "--no-notify"],
            capture_output=True, text=True, env=dict(os.environ), check=False,
        )

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.temps(), [], "the stale temp was never swept")
        self.assertTrue((self.dest / src.name).is_file())
        self.assertEqual((self.dest / src.name).read_bytes(), src.read_bytes())

    def test_a_rerun_after_the_crash_copies_it_properly(self):
        """The recovery that matters: an interrupted copy is not a lost file,
        it is a file that has not been copied yet."""
        src = self.make_payload()
        self.assertTrue(self.copy_until_temp_appears())

        subprocess.run(
            [sys.executable, "-m", "folder_watch", "copy", str(self.src),
             "--to", str(self.dest), "--once", "--catch-up", "--no-done-store",
             "--min-bytes", "1", "--no-notify"],
            capture_output=True, env=dict(os.environ), check=False,
        )

        self.assertEqual((self.dest / src.name).read_bytes(), src.read_bytes())


class TestKilledMidUnzip(CrashCase):
    def make_archive(self, name="delivery.zip") -> Path:
        z = self.src / name
        payload = os.urandom(1 << 20) * PAYLOAD_MB
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("asset.mov", payload)
            zf.writestr("notes.txt", b"second member")
        return z

    def unzip_in_flight(self, target: Path) -> bool:
        """A member is being written, not merely that the directory exists.

        The directory is created just before extractall, so killing on its
        appearance lands before anything is extracted — early enough that even
        an unzip which deleted the archive first would look correct.
        """
        try:
            return any(p.is_file() and p.stat().st_size > 0 for p in target.iterdir())
        except OSError:
            return False

    def unzip_until_extracting(self, z: Path) -> bool:
        target = self.src / z.stem
        self.start(
            "unzip", str(self.src), "--mode", "named",
            "--once", "--catch-up", "--no-done-store",
        )
        return self.kill_when(lambda: self.unzip_in_flight(target))

    def test_the_archive_is_kept(self):
        """The data-loss case. unzip deletes the archive only after every
        member is verified present at its declared size, so a crash anywhere
        before that leaves the only copy of the content where it was."""
        z = self.make_archive()

        self.assertTrue(self.unzip_until_extracting(z), "never caught it extracting")

        self.assertTrue(z.is_file(), "the archive was deleted despite the crash")
        self.assertTrue(zipfile.is_zipfile(z), "the archive was damaged")

    def test_a_half_extracted_directory_does_not_read_as_finished(self):
        """A crash leaves whatever extractall had written. That directory must
        not satisfy the "already done" check, or the next scan skips the
        archive forever and the extraction is silently never completed."""
        from folder_watch.actions.unzip import UnzipAction

        z = self.make_archive()
        self.assertTrue(self.unzip_until_extracting(z))

        action = UnzipAction(mode="named", notify_on_done=False)
        self.assertIsNot(
            action.output_exists(z),
            True,
            "a killed extraction is being reported as complete",
        )

    def test_a_rerun_after_the_crash_extracts_it_properly(self):
        z = self.make_archive()
        self.assertTrue(self.unzip_until_extracting(z))

        subprocess.run(
            [sys.executable, "-m", "folder_watch", "unzip", str(self.src),
             "--mode", "named", "--once", "--catch-up", "--no-done-store",
             "--no-notify"],
            capture_output=True, env=dict(os.environ), check=False,
        )

        target = self.src / "delivery"
        self.assertTrue((target / "asset.mov").is_file())
        self.assertTrue((target / "notes.txt").is_file())
        self.assertFalse(z.exists(), "the archive should go once it is verified")


if __name__ == "__main__":
    unittest.main()
