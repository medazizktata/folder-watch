#!/usr/bin/env python3
"""Tests for folder_watch core + actions."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from folder_watch import (  # noqa: E402
    CopyAction,
    UnzipAction,
    match_ext,
    parse_exts,
    run_watch,
)


class TestHelpers(unittest.TestCase):
    def test_match_ext(self):
        self.assertTrue(match_ext("a.mp4", ["mp4"]))
        self.assertTrue(match_ext("a.MP4", ["mp4"]))
        self.assertFalse(match_ext("a.mov", ["mp4"]))
        self.assertTrue(match_ext("a.mov", None))

    def test_parse_exts(self):
        self.assertIsNone(parse_exts("*"))
        self.assertEqual(parse_exts("mp4,mov"), ["mp4", "mov"])


class TestCopyAction(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_and_skip(self):
        import folder_watch.actions.copy as copy_mod

        f = self.src / "clip.mp4"
        f.write_bytes(b"x" * 100)
        notes: list[tuple[str, str]] = []
        old = copy_mod.notify
        copy_mod.notify = lambda t, b, **_k: notes.append((t, b))
        try:
            action = CopyAction(self.dest, notify_on_done=True)
            self.assertEqual(action.run(f), "copied")
            self.assertTrue((self.dest / "clip.mp4").is_file())
            self.assertEqual(len(notes), 1)
            self.assertIn("clip.mp4", notes[0][1])
            self.assertTrue(f.is_file())
            self.assertEqual(action.run(f), "skip")
            self.assertEqual(len(notes), 1)
        finally:
            copy_mod.notify = old

    def test_copy_via_watch_once(self):
        f = self.src / "a.mp4"
        f.write_bytes(b"hello-world")
        action = CopyAction(self.dest, notify_on_done=False)
        code = run_watch(
            self.src,
            action,
            exts=["mp4"],
            poll=0.1,
            stable_needed=1,
            min_bytes=1,
            once=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual((self.dest / "a.mp4").read_bytes(), b"hello-world")


class TestUnzipAction(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_zip(self, name: str, payload: str = "payload.txt", content: bytes = b"hi") -> Path:
        payload_path = self.tmp / payload
        payload_path.write_bytes(content)
        zpath = self.inbox / name
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.write(payload_path, arcname=payload)
        return zpath

    def test_mode_named_default(self):
        z = self._make_zip("demo.zip")
        action = UnzipAction(mode="named")
        self.assertEqual(action.run(z), "extracted")
        self.assertFalse(z.exists())
        out = self.inbox / "demo" / "payload.txt"
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), b"hi")

    def test_mode_same(self):
        z = self._make_zip("flat.zip", payload="flat.txt", content=b"flat")
        action = UnzipAction(mode="same")
        self.assertEqual(action.run(z), "extracted")
        self.assertFalse(z.exists())
        self.assertTrue((self.inbox / "flat.txt").is_file())

    def test_mode_to(self):
        z = self._make_zip("pack.zip", payload="p.txt", content=b"p")
        dest = self.tmp / "unpacked"
        action = UnzipAction(mode="to", to=dest)
        self.assertEqual(action.run(z), "extracted")
        self.assertFalse(z.exists())
        self.assertTrue((dest / "pack" / "p.txt").is_file())

    def test_bad_zip_not_deleted(self):
        bad = self.inbox / "bad.zip"
        bad.write_bytes(b"not-a-zip")
        action = UnzipAction(mode="named")
        self.assertEqual(action.run(bad), "fail")
        self.assertTrue(bad.exists())

    def test_path_traversal_blocked(self):
        evil = self.inbox / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../outside.txt", "nope")
        action = UnzipAction(mode="named")
        self.assertEqual(action.run(evil), "fail")
        self.assertTrue(evil.exists())
        self.assertFalse((self.tmp / "outside.txt").exists())

    def test_watch_unzip_once(self):
        self._make_zip("w.zip", payload="w.txt", content=b"watch")
        action = UnzipAction(mode="named")
        code = run_watch(
            self.inbox,
            action,
            exts=["zip"],
            poll=0.1,
            stable_needed=1,
            min_bytes=1,
            once=True,
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.inbox / "w.zip").exists())
        self.assertTrue((self.inbox / "w" / "w.txt").is_file())


class TestCLI(unittest.TestCase):
    def test_list_actions(self):
        from folder_watch import main

        self.assertEqual(main(["--list-actions"]), 0)

    def test_copy_cli_once(self):
        from folder_watch import main

        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "src"
            dest = tmp / "dest"
            src.mkdir()
            dest.mkdir()
            (src / "v.mp4").write_bytes(b"12345")
            code = main(
                [
                    "copy",
                    str(src),
                    "--to",
                    str(dest),
                    "--once",
                    "--stable",
                    "1",
                    "--min-bytes",
                    "1",
                    "--poll",
                    "0.1",
                    "--ext",
                    "mp4",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue((dest / "v.mp4").is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unzip_cli_named(self):
        from folder_watch import main

        tmp = Path(tempfile.mkdtemp())
        try:
            inbox = tmp / "inbox"
            inbox.mkdir()
            payload = tmp / "a.txt"
            payload.write_text("ok")
            z = inbox / "t.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.write(payload, arcname="a.txt")
            code = main(
                [
                    "unzip",
                    str(inbox),
                    "--once",
                    "--stable",
                    "1",
                    "--min-bytes",
                    "1",
                    "--poll",
                    "0.1",
                ]
            )
            self.assertEqual(code, 0)
            self.assertFalse(z.exists())
            self.assertTrue((inbox / "t" / "a.txt").is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestConvxWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_format_or_preset(self):
        from folder_watch import build_convx_watch_cmd

        with self.assertRaises(ValueError):
            build_convx_watch_cmd(self.src)

    def test_build_maps_flags(self):
        from folder_watch import build_convx_watch_cmd

        cmd = build_convx_watch_cmd(
            self.src,
            format="webp",
            convx_preset="web-image",
            quality=80,
            width=1280,
            fps=24.0,
            max_size=1_000_000,
            overwrite=True,
            filter_exts=["png", "jpg"],
            debounce_ms=250,
            convx_bin="/fake/convx",
        )
        self.assertEqual(cmd[0], "/fake/convx")
        self.assertEqual(cmd[1], "watch")
        self.assertEqual(Path(cmd[2]), self.src.resolve())
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "webp")
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "web-image")
        self.assertIn("--filter", cmd)
        self.assertEqual(cmd[cmd.index("--filter") + 1], "*.png,*.jpg")
        self.assertIn("--debounce", cmd)
        self.assertEqual(cmd[cmd.index("--debounce") + 1], "250")
        self.assertIn("-q", cmd)
        self.assertIn("-w", cmd)
        self.assertIn("--fps", cmd)
        self.assertIn("--max-size", cmd)
        self.assertIn("--overwrite", cmd)

    def test_no_overwrite_omits_flag(self):
        from folder_watch import build_convx_watch_cmd

        cmd = build_convx_watch_cmd(self.src, format="mp3", overwrite=False)
        self.assertNotIn("--overwrite", cmd)

    def test_cli_rejects_to_and_once(self):
        from folder_watch import main as fw_main

        self.assertEqual(
            fw_main(["convx", str(self.src), "--format", "webp", "--to", "/tmp/x"]),
            2,
        )
        self.assertEqual(
            fw_main(["convx", str(self.src), "--format", "webp", "--once"]),
            2,
        )


class TestDryRunAndPartials(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partial_names(self):
        from folder_watch.support.partials import is_partial_filename
        from folder_watch import should_watch_name

        self.assertTrue(is_partial_filename("Video.mp4.part"))
        self.assertTrue(is_partial_filename("foo.tmp"))
        self.assertTrue(is_partial_filename("._Video.mp4"))
        self.assertTrue(is_partial_filename("~$draft.docx"))
        self.assertFalse(is_partial_filename("Video-1-v4.mp4"))
        self.assertFalse(should_watch_name("a.mp4.part", ["mp4"]))
        self.assertTrue(should_watch_name("a.mp4", ["mp4"]))

    def test_dry_run_copy(self):
        from folder_watch import CopyAction

        f = self.src / "a.mp4"
        f.write_bytes(b"data")
        action = CopyAction(self.dest, notify_on_done=False, dry_run=True)
        self.assertEqual(action.run(f), "would_copy")
        self.assertFalse((self.dest / "a.mp4").exists())


class TestDoneStoreAndAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_done_store_roundtrip(self):
        from folder_watch.support.state import DoneStore, file_identity

        store = DoneStore(self.tmp / "done.json")
        src = self.tmp / "a.mp4"
        dest = self.tmp / "out" / "a.mp4"
        src.write_bytes(b"x" * 50)
        dest.parent.mkdir()
        size, mtime = file_identity(src)
        key = DoneStore.make_key(
            "copy", src, size=size, mtime_ns=mtime, dest=dest
        )
        self.assertFalse(store.is_done(key))
        store.mark(key, name="a.mp4")
        store2 = DoneStore(self.tmp / "done.json")
        self.assertTrue(store2.is_done(key))

    def test_atomic_copy(self):
        from folder_watch.support.copy_io import atomic_copy

        src = self.tmp / "src.bin"
        dest = self.tmp / "dest" / "out.bin"
        src.write_bytes(b"hello-atomic")
        atomic_copy(src, dest, retries=2)
        self.assertEqual(dest.read_bytes(), b"hello-atomic")
        self.assertFalse(any(self.tmp.joinpath("dest").glob("*.tmp")))


class TestSettleNonBlocking(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_file_stays_waiting(self):
        from folder_watch import settle_step
        import folder_watch.core.watch as watch_mod

        f = self.tmp / "enc.mp4"
        f.write_bytes(b"x" * 100)
        track: dict = {}
        old = watch_mod.is_open
        watch_mod.is_open = lambda _p: 1
        try:
            # Size never moves, so the lsof gate is reached and must keep
            # returning waiting for as long as a writer holds the file.
            for _ in range(6):
                self.assertEqual(
                    settle_step(f, track, min_bytes=1, stable_needed=3), "waiting"
                )
            self.assertNotIn("size", track)
        finally:
            watch_mod.is_open = old

    def test_lsof_not_called_while_file_grows(self):
        """lsof costs ~250ms; a growing file is proof enough of a writer."""
        from folder_watch import settle_step
        import folder_watch.core.watch as watch_mod

        f = self.tmp / "growing.mp4"
        calls: list[Path] = []
        old = watch_mod.is_open
        watch_mod.is_open = lambda p: calls.append(p) or 0
        try:
            for n in range(1, 6):
                f.write_bytes(b"x" * (n * 100))
                self.assertEqual(
                    settle_step(f, track := {}, min_bytes=1, stable_needed=3),
                    "waiting",
                )
            self.assertEqual(calls, [], "lsof called on a still-growing file")

            # Plateau: same size three ticks running reaches the gate.
            track = {}
            for _ in range(2):
                self.assertEqual(
                    settle_step(f, track, min_bytes=1, stable_needed=3), "waiting"
                )
            self.assertEqual(calls, [], "lsof called before the size plateaued")
            self.assertEqual(
                settle_step(f, track, min_bytes=1, stable_needed=3), "ready"
            )
            self.assertEqual(len(calls), 1)
        finally:
            watch_mod.is_open = old

    def test_stall_clock_tracks_size_changes(self):
        """last_change drives give_up; it must only move when the file does."""
        from folder_watch import settle_step
        import folder_watch.core.watch as watch_mod

        f = self.tmp / "enc.mp4"
        f.write_bytes(b"x" * 100)
        track: dict = {}
        old = watch_mod.is_open
        watch_mod.is_open = lambda _p: 1
        try:
            settle_step(f, track, min_bytes=1, stable_needed=3)
            first = track["last_change"]

            # Size holds still — clock must not move, so a stuck file ages out.
            for _ in range(3):
                settle_step(f, track, min_bytes=1, stable_needed=3)
            self.assertEqual(track["last_change"], first)

            # Real growth resets it, so a slow encode is never given up on.
            f.write_bytes(b"x" * 200)
            settle_step(f, track, min_bytes=1, stable_needed=3)
            self.assertGreater(track["last_change"], first)
        finally:
            watch_mod.is_open = old

    def test_stalled_zero_byte_file_ages_out(self):
        """The Video-5-v2.mp4 case: 0 bytes forever must not watch forever."""
        from folder_watch import settle_step
        import folder_watch.core.watch as watch_mod

        f = self.tmp / "aborted.mp4"
        f.write_bytes(b"")
        track: dict = {}
        old = watch_mod.is_open
        watch_mod.is_open = lambda _p: 0
        try:
            for _ in range(4):
                self.assertEqual(
                    settle_step(f, track, min_bytes=1_000_000, stable_needed=3),
                    "waiting",
                )
            # The event loop gives up once now - last_change > STALL_TIMEOUT.
            stalled = time.monotonic() - track["last_change"]
            self.assertLess(stalled, watch_mod.STALL_TIMEOUT)
            track["last_change"] = time.monotonic() - watch_mod.STALL_TIMEOUT - 1
            self.assertGreater(
                time.monotonic() - track["last_change"], watch_mod.STALL_TIMEOUT
            )
        finally:
            watch_mod.is_open = old

    def test_below_min_bytes_skips_lsof(self):
        """The 0-byte stall case: don't burn an lsof every tick on it."""
        from folder_watch import settle_step
        import folder_watch.core.watch as watch_mod

        f = self.tmp / "stalled.mp4"
        f.write_bytes(b"")
        calls: list[Path] = []
        old = watch_mod.is_open
        watch_mod.is_open = lambda p: calls.append(p) or 0
        try:
            track: dict = {}
            for _ in range(5):
                self.assertEqual(
                    settle_step(f, track, min_bytes=1_000_000, stable_needed=3),
                    "waiting",
                )
            self.assertEqual(calls, [])
        finally:
            watch_mod.is_open = old

    def test_closed_file_reaches_ready(self):
        from folder_watch import settle_step
        import folder_watch.core.watch as watch_mod

        f = self.tmp / "done.mp4"
        f.write_bytes(b"x" * 100)
        track: dict = {}
        old = watch_mod.is_open
        watch_mod.is_open = lambda _p: 0
        try:
            for _ in range(2):
                self.assertEqual(
                    settle_step(f, track, min_bytes=1, stable_needed=3), "waiting"
                )
            self.assertEqual(
                settle_step(f, track, min_bytes=1, stable_needed=3), "ready"
            )
            self.assertEqual(track["size"], 100)
        finally:
            watch_mod.is_open = old


if __name__ == "__main__":
    unittest.main()
