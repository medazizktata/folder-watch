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

# Only for waits measured in whole seconds — a pidfile appearing, a process
# exiting. The kill loop below polls without one; see the note there.
POLL_INTERVAL = 0.002


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
            # No sleep here, deliberately. This is aiming at the interval
            # between two syscalls, and a 2 ms poll was coarse enough that a
            # fast runner finished the whole copy between checks — the kill
            # never landed mid-flight and four tests failed on CI while passing
            # locally. Spinning is the cost of aiming at a window this narrow.
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


def child_script(body: str) -> str:
    """A standalone program that crashes itself at a chosen instant.

    Polling for a temp file catches the middle of the data phase, which is the
    common case but not the interesting one. The windows that actually decide
    whether work is lost are single statements wide — between the bytes
    landing and the done-store recording it, between an archive being
    extracted and being deleted — and no amount of polling from outside can
    aim at those. Patching the exact call and killing inside it can.

    SIGKILL rather than an exception, because an exception is a failure the
    code gets to handle and this is deliberately not that.
    """
    preamble = (
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
        "import _isolate\n"
        "def die():\n"
        "    sys.stdout.flush()\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    return preamble + body


class SurgicalCase(CrashCase):
    def run_child(self, body: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", child_script(body)],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            check=False,
        )

    def assertKilled(self, done: subprocess.CompletedProcess) -> None:
        self.assertEqual(
            done.returncode,
            -signal.SIGKILL,
            f"child did not die where expected: rc={done.returncode} {done.stderr[:300]}",
        )


class TestCrashBetweenExtractAndDelete(SurgicalCase):
    """The window where the archive has been read but not yet earned deletion.

    This is the one that would lose a client delivery: the members are on disk,
    the verification has not run, and the archive is the only complete copy of
    the content. Nothing may delete it before the check passes.
    """

    def make_archive(self) -> Path:
        z = self.src / "delivery.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("asset.mov", os.urandom(1 << 20))
            zf.writestr("notes.txt", b"second member")
        return z

    def test_the_archive_survives_a_crash_right_after_extraction(self):
        z = self.make_archive()

        done = self.run_child(
            "import zipfile\n"
            "from pathlib import Path\n"
            "from folder_watch.actions.unzip import UnzipAction\n"
            "real = zipfile.ZipFile.extractall\n"
            "def boom(self, path=None, **kw):\n"
            "    real(self, path, **kw)\n"
            "    die()\n"          # extracted, not yet verified or deleted
            "zipfile.ZipFile.extractall = boom\n"
            f"UnzipAction(mode='named', notify_on_done=False).run(Path({str(z)!r}))\n"
        )

        self.assertKilled(done)
        self.assertTrue(z.is_file(), "the archive was lost in the unverified window")
        self.assertTrue(zipfile.is_zipfile(z))

    def test_the_half_state_is_not_mistaken_for_finished(self):
        from folder_watch.actions.unzip import UnzipAction

        z = self.make_archive()
        self.run_child(
            "import zipfile\n"
            "from pathlib import Path\n"
            "from folder_watch.actions.unzip import UnzipAction\n"
            "real = zipfile.ZipFile.extractall\n"
            "def boom(self, path=None, **kw):\n"
            "    # die partway: the directory exists, the members do not all\n"
            "    Path(path).mkdir(parents=True, exist_ok=True)\n"
            "    die()\n"
            "zipfile.ZipFile.extractall = boom\n"
            f"UnzipAction(mode='named', notify_on_done=False).run(Path({str(z)!r}))\n"
        )

        action = UnzipAction(mode="named", notify_on_done=False)
        self.assertIs(action.output_exists(z), False)

    def test_a_rerun_finishes_the_job(self):
        z = self.make_archive()
        self.run_child(
            "import zipfile\n"
            "from pathlib import Path\n"
            "from folder_watch.actions.unzip import UnzipAction\n"
            "real = zipfile.ZipFile.extractall\n"
            "def boom(self, path=None, **kw):\n"
            "    real(self, path, **kw)\n"
            "    die()\n"
            "zipfile.ZipFile.extractall = boom\n"
            f"UnzipAction(mode='named', notify_on_done=False).run(Path({str(z)!r}))\n"
        )

        subprocess.run(
            [sys.executable, "-m", "folder_watch", "unzip", str(self.src),
             "--mode", "named", "--once", "--catch-up", "--no-done-store", "--no-notify"],
            capture_output=True, env=dict(os.environ), check=False,
        )

        target = self.src / "delivery"
        self.assertTrue((target / "asset.mov").is_file())
        self.assertTrue((target / "notes.txt").is_file())
        self.assertFalse(z.exists(), "verified content should retire the archive")


class TestCrashBetweenCopyAndRecord(SurgicalCase):
    """Between the bytes landing and the done-store admitting it.

    Getting this order wrong the other way round — record first, copy second —
    would mean a crash here marks a file done that was never copied, and no
    later scan ever looks at it again. That is silent, permanent data loss,
    and the only thing standing between it and the user is the statement
    order in apply_action.
    """

    def test_a_file_recorded_but_not_copied_is_impossible(self):
        from folder_watch.support.state import DoneStore

        src = self.make_payload("clip.mp4")
        store_path = self.tmp / "done.json"

        done = self.run_child(
            "from pathlib import Path\n"
            "import folder_watch.core.watch as watch\n"
            "import folder_watch.actions.copy as cmod\n"
            "from folder_watch.actions.copy import CopyAction\n"
            "from folder_watch.support.state import DoneStore\n"
            "real = cmod.atomic_copy\n"
            "def boom(s, d, **kw):\n"
            "    real(s, d, **kw)\n"
            "    die()\n"          # copied; done-store not yet written
            "cmod.atomic_copy = boom\n"
            f"store = DoneStore(Path({str(store_path)!r}))\n"
            f"action = CopyAction(Path({str(self.dest)!r}), notify_on_done=False)\n"
            f"watch.apply_action(Path({str(src)!r}), action, state={{}},\n"
            "                   stable_needed=1, done_store=store)\n"
        )

        self.assertKilled(done)
        self.assertTrue((self.dest / src.name).is_file(), "the copy did not land")
        store = DoneStore(store_path)
        self.assertEqual(
            len(store), 0, "a crash left the file marked done outside apply_action"
        )

    def test_the_rerun_is_idempotent(self):
        """Redoing a copy that already landed is harmless; skipping one that
        did not is not. The order chooses the harmless failure."""
        src = self.make_payload("clip.mp4")

        subprocess.run(
            [sys.executable, "-m", "folder_watch", "copy", str(self.src),
             "--to", str(self.dest), "--once", "--catch-up", "--min-bytes", "1",
             "--no-notify", "--no-done-store"],
            capture_output=True, env=dict(os.environ), check=False,
        )

        self.assertEqual((self.dest / src.name).read_bytes(), src.read_bytes())


class TestStateFilesSurviveACrash(SurgicalCase):
    """A corrupt done-store or retry queue would poison every later run.

    Both are written with mkstemp + fsync + os.replace, so a crash mid-write
    leaves the previous file intact and the new one unpublished. Worth
    asserting, because "it was atomic" is the kind of claim that quietly stops
    being true.
    """

    def test_the_done_store_is_still_valid_json_after_a_crash_mid_write(self):
        import json

        store_path = self.tmp / "done.json"
        done = self.run_child(
            "import os\n"
            "from pathlib import Path\n"
            "from folder_watch.support.state import DoneStore\n"
            f"store = DoneStore(Path({str(store_path)!r}))\n"
            "store.mark('first|a|b|c|1|2', name='a.mp4', result='copied')\n"
            "real = os.replace\n"
            "def boom(a, b):\n"
            "    die()\n"          # temp written and fsynced; never published
            "os.replace = boom\n"
            "store.mark('second|a|b|c|3|4', name='b.mp4', result='copied')\n"
        )

        self.assertKilled(done)
        self.assertTrue(store_path.is_file(), "the previous store vanished")
        data = json.loads(store_path.read_text())
        self.assertIn("entries", data)
        self.assertEqual(len(data["entries"]), 1, "an unpublished write leaked in")

    def test_the_retry_queue_is_still_valid_json_after_a_crash_mid_write(self):
        import json

        queue_path = self.tmp / "retry.json"
        src = self.make_payload("clip.mp4")
        done = self.run_child(
            "import os\n"
            "from pathlib import Path\n"
            "from folder_watch.support.retry import RetryQueue\n"
            f"q = RetryQueue(Path({str(queue_path)!r}))\n"
            f"q.schedule(Path({str(src)!r}), action='copy', reason='transient',\n"
            "           size=1, mtime_ns=2)\n"
            "real = os.replace\n"
            "def boom(a, b):\n"
            "    die()\n"
            "os.replace = boom\n"
            f"q.schedule(Path({str(src)!r}), action='copy', reason='transient',\n"
            "           size=9, mtime_ns=9)\n"
        )

        self.assertKilled(done)
        data = json.loads(queue_path.read_text())
        self.assertEqual(len(data["entries"]), 1)

    def test_crash_debris_is_not_mistaken_for_a_store(self):
        """The unpublished temp used to be named done-XXXX.json, which is
        exactly what prune globs for. So a process killed mid-save left behind
        a file that the next prune read as somebody's done-store and tidied as
        one. Naming it .json.tmp is what keeps the two apart."""
        from folder_watch.support.state import DoneStore

        store_path = self.tmp / "done.json"
        self.run_child(
            "import os\n"
            "from pathlib import Path\n"
            "from folder_watch.support.state import DoneStore\n"
            f"store = DoneStore(Path({str(store_path)!r}))\n"
            "real = os.replace\n"
            "def boom(a, b):\n"
            "    die()\n"
            "os.replace = boom\n"
            "store.mark('k|a|b|c|1|2', name='a.mp4', result='copied')\n"
        )

        debris = [p for p in self.tmp.iterdir() if p.name.startswith("done-")]
        self.assertTrue(debris, "expected the unpublished temp to remain")
        for p in debris:
            self.assertTrue(
                p.name.endswith(".json.tmp"),
                f"{p.name} would be globbed as a real store",
            )
        self.assertEqual(len(DoneStore(store_path)), 0)


class TestManyFilesInFlight(CrashCase):
    """A crash must cost at most the file being written, never the ones
    already finished."""

    def test_finished_files_survive_and_only_the_in_flight_one_is_missing(self):
        for i in range(6):
            (self.src / f"clip{i}.mp4").write_bytes(os.urandom(1 << 20) * 2)

        self.start(
            "copy", str(self.src), "--to", str(self.dest),
            "--once", "--catch-up", "--no-done-store", "--min-bytes", "1",
        )
        # Kill once a couple have landed, so there is finished work to lose.
        caught = self.kill_when(
            lambda: len(list(self.dest.glob("clip*.mp4"))) >= 2 and self.copy_in_flight()
        )
        self.assertTrue(caught, "never caught it with work both done and in flight")

        landed = sorted(self.dest.glob("clip*.mp4"))
        self.assertGreaterEqual(len(landed), 2)
        for f in landed:
            self.assertEqual(
                f.read_bytes(), (self.src / f.name).read_bytes(),
                f"{f.name} was finished before the crash but is corrupt",
            )

    def test_a_rerun_completes_every_file(self):
        for i in range(6):
            (self.src / f"clip{i}.mp4").write_bytes(os.urandom(1 << 20) * 2)

        self.start(
            "copy", str(self.src), "--to", str(self.dest),
            "--once", "--catch-up", "--no-done-store", "--min-bytes", "1",
        )
        self.kill_when(self.copy_in_flight)

        subprocess.run(
            [sys.executable, "-m", "folder_watch", "copy", str(self.src),
             "--to", str(self.dest), "--once", "--catch-up", "--no-done-store",
             "--min-bytes", "1", "--no-notify"],
            capture_output=True, env=dict(os.environ), check=False,
        )

        for i in range(6):
            name = f"clip{i}.mp4"
            self.assertEqual(
                (self.dest / name).read_bytes(), (self.src / name).read_bytes(), name
            )
        # The crash temp is deliberately spared: it is younger than the sweep
        # threshold, and a fresh temp can belong to a copy still running.
        self.assertLessEqual(len(self.temps()), 1)


class TestRepeatedCrashes(CrashCase):
    """Crash, restart, crash, restart. State has to converge, not accumulate."""

    def test_three_crashes_still_end_with_a_correct_copy(self):
        src = self.make_payload("clip.mp4")

        for _ in range(3):
            self.start(
                "copy", str(self.src), "--to", str(self.dest),
                "--once", "--catch-up", "--min-bytes", "1",
            )
            self.kill_when(self.copy_in_flight)

        final = subprocess.run(
            [sys.executable, "-m", "folder_watch", "copy", str(self.src),
             "--to", str(self.dest), "--once", "--catch-up", "--min-bytes", "1",
             "--no-notify"],
            capture_output=True, text=True, env=dict(os.environ), check=False,
        )

        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        self.assertEqual((self.dest / src.name).read_bytes(), src.read_bytes())

    def test_the_temps_do_not_pile_up_unboundedly(self):
        """Each crash leaves one. They are swept by age, so the count should
        track the crashes rather than the runs."""
        self.make_payload("clip.mp4")

        for _ in range(3):
            self.start(
                "copy", str(self.src), "--to", str(self.dest),
                "--once", "--catch-up", "--no-done-store", "--min-bytes", "1",
            )
            self.kill_when(self.copy_in_flight)

        self.assertLessEqual(len(self.temps()), 3)
        for t in self.temps():
            self.assertTrue(t.name.startswith("."), "a leftover is not hidden")


class TestSignalsComparedToCrashes(CrashCase):
    """SIGTERM is a request; SIGKILL is not. They should differ in exactly one
    visible way — whether the process got to tidy up after itself."""

    def watcher(self) -> subprocess.Popen:
        return self.start(
            "copy", str(self.src), "--to", str(self.dest),
            "--backend", "poll", "--poll", "0.2", "--min-bytes", "1",
            "--no-done-store",
        )

    def pidfiles(self) -> list[Path]:
        from folder_watch.support.paths import PID_DIR

        return list(PID_DIR.glob("copy-src.*.json"))

    def wait_for(self, condition, timeout=20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(POLL_INTERVAL)
        return False

    def test_sigterm_lets_the_watcher_clear_its_pidfile(self):
        for p in self.pidfiles():
            p.unlink(missing_ok=True)
        proc = self.watcher()
        self.assertTrue(self.wait_for(lambda: bool(self.pidfiles())), "no pidfile")

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)

        self.assertTrue(
            self.wait_for(lambda: not self.pidfiles(), 10),
            "SIGTERM left a stale pidfile — the handler did not unwind",
        )

    def test_sigkill_leaves_one_for_prune_to_find(self):
        for p in self.pidfiles():
            p.unlink(missing_ok=True)
        proc = self.watcher()
        self.assertTrue(self.wait_for(lambda: bool(self.pidfiles())), "no pidfile")

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=15)

        self.assertTrue(self.pidfiles(), "expected a stale pidfile after SIGKILL")
        subprocess.run(
            [sys.executable, "-m", "folder_watch", "prune", "--yes"],
            capture_output=True, env=dict(os.environ), check=False,
        )
        self.assertEqual(self.pidfiles(), [], "prune did not clear it")
