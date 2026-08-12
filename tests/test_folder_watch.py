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

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import (
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

    def _make_zip(
        self, name: str, payload: str = "payload.txt", content: bytes = b"hi"
    ) -> Path:
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

    def test_missing_dest_parent_is_not_conjured(self):
        """An unmounted drive must not be rebuilt as local folders."""
        from folder_watch.support.paths import DestUnavailable, ensure_dest_dir

        missing = self.tmp / "NotMounted" / "Drive" / "Project" / "Output"
        with self.assertRaises(DestUnavailable):
            ensure_dest_dir(missing)
        self.assertFalse((self.tmp / "NotMounted").exists())

        # An existing parent means the leaf is just a folder we haven't made yet.
        ok = self.dest / "V2"
        ensure_dest_dir(ok)
        self.assertTrue(ok.is_dir())

        # --create-dest is the explicit opt-in to the old behaviour.
        ensure_dest_dir(missing, create_parents=True)
        self.assertTrue(missing.is_dir())

    def test_copy_to_unmounted_dest_fails_and_stays_retryable(self):
        """The bad case: copied to local disk, marked done, never retried."""
        from folder_watch.support.state import DoneStore, file_identity

        src = self.src / "clip.mp4"
        src.write_bytes(b"x" * 128)
        gone = self.tmp / "NotMounted" / "Drive" / "Output"
        action = CopyAction(gone, notify_on_done=False)

        # "retry" rather than "fail": the action declares this transient so the
        # retry queue owns it. Either way it must not be marked done.
        self.assertEqual(action.run(src), "retry")
        self.assertFalse((self.tmp / "NotMounted").exists(), "rebuilt the tree")

        # "fail" must never reach the done-store, or the retry gets skipped.
        store = DoneStore(self.tmp / "done.json")
        size, mtime_ns = file_identity(src)
        key = DoneStore.make_key(
            "copy", src, size=size, mtime_ns=mtime_ns, dest=gone / src.name
        )
        self.assertFalse(store.is_done(key))

        # Once the volume is back, the same file copies normally.
        gone.mkdir(parents=True)
        self.assertEqual(action.run(src), "copied")
        self.assertEqual((gone / "clip.mp4").read_bytes(), b"x" * 128)

    def test_done_store_prunes_vanished_sources(self):
        from folder_watch.support.state import DoneStore, file_identity

        store = DoneStore(self.tmp / "prune.json")
        alive = self.src / "alive.mp4"
        dead = self.src / "dead.mp4"
        for f in (alive, dead):
            f.write_bytes(b"x" * 10)

        keys = {}
        for f in (alive, dead):
            size, mtime_ns = file_identity(f)
            keys[f] = DoneStore.make_key("copy", f, size=size, mtime_ns=mtime_ns)
            store.mark(keys[f], name=f.name)
        self.assertEqual(len(store), 2)

        dead.unlink()
        self.assertEqual(store.prune(), 1)
        self.assertTrue(store.is_done(keys[alive]))
        self.assertFalse(store.is_done(keys[dead]))

        # Survives a reload — pruning is persisted, not just in memory.
        self.assertEqual(len(DoneStore(self.tmp / "prune.json")), 1)

    def test_empty_done_store_is_truthy(self):
        """__len__ would otherwise make a fresh store read as 'not configured'."""
        from folder_watch.support.state import DoneStore

        empty = DoneStore(self.tmp / "fresh.json")
        self.assertEqual(len(empty), 0)
        self.assertTrue(empty)
        self.assertTrue(bool(empty))

    def test_env_parsing(self):
        from folder_watch.support.env import parse_env

        parsed = parse_env(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "PLAIN=value",
                    "export EXPORTED=yes",
                    'QUOTED="has spaces"',
                    "SINGLE='single'",
                    "EMPTY=",
                    "INNER=Videos /V2",  # interior spaces must survive
                    "no_equals_sign",
                ]
            )
        )
        self.assertEqual(parsed["PLAIN"], "value")
        self.assertEqual(parsed["EXPORTED"], "yes")
        self.assertEqual(parsed["QUOTED"], "has spaces")
        self.assertEqual(parsed["SINGLE"], "single")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertEqual(parsed["INNER"], "Videos /V2")
        self.assertNotIn("no_equals_sign", parsed)

    def test_env_prefers_real_environment(self):
        import os

        from folder_watch.support.env import env

        key = "FOLDER_WATCH_TEST_ONLY_VALUE"
        os.environ[key] = "from-environ"
        try:
            self.assertEqual(env(key), "from-environ")
        finally:
            del os.environ[key]
        self.assertEqual(env(key, "fallback"), "fallback")

    def test_drive_root_unset_yields_no_default_preset(self):
        """No .env must not mean a broken import — just no built-in preset."""
        import folder_watch.presets as presets_mod

        real = presets_mod.env
        presets_mod.env = lambda _name, default=None: default
        try:
            self.assertIsNone(presets_mod.drive_root())
            self.assertEqual(presets_mod._defaults(), {})
        finally:
            presets_mod.env = real

    def test_partial_names(self):
        from folder_watch import should_watch_name
        from folder_watch.support.partials import is_partial_filename

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
        # The caller owns dest.parent now — atomic_copy creating it would
        # rebuild an unmounted volume's tree on local disk.
        dest.parent.mkdir(parents=True)
        atomic_copy(src, dest, retries=2)
        self.assertEqual(dest.read_bytes(), b"hello-atomic")
        self.assertFalse(any(self.tmp.joinpath("dest").glob("*.tmp")))


class TestBaselinePending(unittest.TestCase):
    """Baseline skips what's already there; it must not skip silently."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_output_exists(self):
        f = self.src / "clip.mp4"
        f.write_bytes(b"x" * 64)
        action = CopyAction(self.dest, notify_on_done=False)

        self.assertIs(action.output_exists(f), False)
        (self.dest / "clip.mp4").write_bytes(b"x" * 64)
        self.assertIs(action.output_exists(f), True)
        # A partial/mismatched copy is not an output.
        (self.dest / "clip.mp4").write_bytes(b"x" * 10)
        self.assertIs(action.output_exists(f), False)

    def test_unzip_output_exists(self):
        z = self.src / "pack.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "a")
        action = UnzipAction(mode="named")

        self.assertIs(action.output_exists(z), False)
        target = self.src / "pack"
        target.mkdir()
        self.assertIs(action.output_exists(z), False, "empty dir is not output")
        (target / "a.txt").write_text("a")
        self.assertIs(action.output_exists(z), True)

        # mode=same extracts beside the zip, so the directory's existence
        # proves nothing — it asks the archive what it should have produced.
        same = UnzipAction(mode="same")
        self.assertIs(same.output_exists(z), False, "blind for mode=same")
        (self.src / "a.txt").write_text("a")
        self.assertIs(same.output_exists(z), True)

    def test_list_pending_is_read_only(self):
        from folder_watch.core.watch import list_pending

        done = self.src / "done.mp4"
        missed = self.src / "missed.mp4"
        for f in (done, missed):
            f.write_bytes(b"x" * 64)
        (self.dest / "done.mp4").write_bytes(b"x" * 64)

        action = CopyAction(self.dest, notify_on_done=False)
        before = sorted(p.name for p in self.tmp.rglob("*"))
        rows = list_pending(self.src, action, exts=["mp4"], min_bytes=1)

        self.assertEqual([p.name for p in rows], ["missed.mp4"])
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)

    def test_seed_baseline_counts_pending(self):
        from folder_watch.core.watch import seed_baseline

        done = self.src / "done.mp4"
        missed = self.src / "missed.mp4"
        for f in (done, missed):
            f.write_bytes(b"x" * 64)
        (self.dest / "done.mp4").write_bytes(b"x" * 64)

        action = CopyAction(self.dest, notify_on_done=False)
        state: dict = {}
        seed_baseline(self.src, action, exts=["mp4"], min_bytes=1, state=state)
        # Both are still skipped — startup must not trigger a surprise run.
        self.assertEqual(len(state), 2)
        self.assertTrue(all(v.get("baseline") for v in state.values()))

    def test_catch_up_clears_baseline_assumptions(self):
        """--catch-up on a folder a watcher already baselined must still work."""
        from folder_watch.support.state import DoneStore, file_identity

        store = DoneStore(self.tmp / "done.json")
        assumed = self.src / "assumed.mp4"
        real = self.src / "real.mp4"
        for f in (assumed, real):
            f.write_bytes(b"x" * 64)

        size, mtime_ns = file_identity(assumed)
        store.mark_baseline_file("copy", assumed, size=size, mtime_ns=mtime_ns)
        size, mtime_ns = file_identity(real)
        real_key = DoneStore.make_key("copy", real, size=size, mtime_ns=mtime_ns)
        store.mark(real_key, name=real.name, result="copied")
        self.assertEqual(len(store), 2)

        self.assertEqual(store.drop_baseline(), 1)
        # The verified result survives; only the assumption goes.
        self.assertTrue(store.is_done(real_key))
        self.assertEqual(len(store), 1)
        self.assertEqual(len(DoneStore(self.tmp / "done.json")), 1)


class TestAuditRegressions(unittest.TestCase):
    """Defects found by independent audit + before/after benchmark."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_size_re_export_is_not_skipped(self):
        """A re-export at identical length must not leave the old copy in place."""
        f = self.src / "take.mp4"
        f.write_bytes(b"V1" + b"x" * 98)
        action = CopyAction(self.dest, notify_on_done=False)
        self.assertEqual(action.run(f), "copied")
        self.assertEqual((self.dest / "take.mp4").read_bytes()[:2], b"V1")

        # Same length, different content — the case size-only comparison missed.
        time.sleep(0.01)
        f.write_bytes(b"V2" + b"y" * 98)
        self.assertEqual(action.run(f), "copied")
        self.assertEqual((self.dest / "take.mp4").read_bytes()[:2], b"V2")

        # Genuinely unchanged still skips.
        self.assertEqual(action.run(f), "skip")

    def test_recursive_copy_mirrors_tree(self):
        """Same basename in two subfolders must not overwrite at the destination."""
        for sub, payload in (("clientA", b"A"), ("clientB", b"B")):
            (self.src / sub).mkdir()
            (self.src / sub / "final.mp4").write_bytes(payload * 64)

        action = CopyAction(self.dest, notify_on_done=False, src_root=self.src)
        for sub in ("clientA", "clientB"):
            self.assertEqual(action.run(self.src / sub / "final.mp4"), "copied")

        self.assertEqual((self.dest / "clientA" / "final.mp4").read_bytes(), b"A" * 64)
        self.assertEqual((self.dest / "clientB" / "final.mp4").read_bytes(), b"B" * 64)

    def test_flat_copy_unchanged_by_mirroring(self):
        """Without subdirectories the relative path is just the name."""
        f = self.src / "clip.mp4"
        f.write_bytes(b"x" * 64)
        action = CopyAction(self.dest, notify_on_done=False, src_root=self.src)
        self.assertEqual(action.done_dest(f), self.dest / "clip.mp4")
        self.assertEqual(action.run(f), "copied")

    def test_stable_one_is_ready_on_first_look(self):
        """--stable 1 means one observation; returning early cost --once a tick."""
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

        f = self.src / "a.mp4"
        f.write_bytes(b"x" * 100)
        old = watch_mod.is_open
        watch_mod.is_open = lambda _p: 0
        try:
            track: dict = {}
            self.assertEqual(
                settle_step(f, track, min_bytes=1, stable_needed=1), "ready"
            )
            # Higher thresholds still take the full count.
            track = {}
            for _ in range(2):
                self.assertEqual(
                    settle_step(f, track, min_bytes=1, stable_needed=3), "waiting"
                )
            self.assertEqual(
                settle_step(f, track, min_bytes=1, stable_needed=3), "ready"
            )
        finally:
            watch_mod.is_open = old

    def test_baseline_probes_unfinished_file_despite_old_mtime(self):
        """An old mtime is not proof of finished — a stalled writer holds on."""
        import os

        import folder_watch.core.watch as watch_mod
        from folder_watch.core.watch import seed_baseline

        held = self.src / "held.mp4"
        held.write_bytes(b"x" * 128)
        old_t = time.time() - 7200
        os.utime(held, (old_t, old_t))

        probed: list[Path] = []
        real = watch_mod.is_open
        watch_mod.is_open = lambda p: probed.append(p) or 1  # writer still on it
        try:
            state: dict = {}
            action = CopyAction(self.dest, notify_on_done=False, src_root=self.src)
            unsettled = seed_baseline(
                self.src, action, exts=["mp4"], min_bytes=1, state=state
            )
            self.assertEqual([p.name for p in probed], ["held.mp4"])
            self.assertEqual([p.name for p in unsettled], ["held.mp4"])
        finally:
            watch_mod.is_open = real

    def test_baseline_skips_probe_when_output_already_exists(self):
        """The startup win: a restart over already-copied files pays no lsof."""
        import os

        import folder_watch.core.watch as watch_mod
        from folder_watch.core.watch import seed_baseline

        f = self.src / "done.mp4"
        f.write_bytes(b"x" * 128)
        action = CopyAction(self.dest, notify_on_done=False, src_root=self.src)
        self.assertEqual(action.run(f), "copied")  # copy2 preserves mtime
        old_t = time.time() - 7200
        os.utime(f, (old_t, old_t))
        os.utime(self.dest / "done.mp4", (old_t, old_t))

        probed: list[Path] = []
        real = watch_mod.is_open
        watch_mod.is_open = lambda p: probed.append(p) or 0
        try:
            unsettled = seed_baseline(
                self.src, action, exts=["mp4"], min_bytes=1, state={}
            )
            self.assertEqual(probed, [], "paid for lsof on a file already copied")
            self.assertEqual(unsettled, [])
        finally:
            watch_mod.is_open = real


class TestAuditSmallFindings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partial_extract_keeps_the_archive(self):
        """any() passed when 1 of N members landed, then deleted the zip."""
        z = self.inbox / "p.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "a")
            zf.writestr("b.txt", "b")

        action = UnzipAction(mode="named")
        target = self.inbox / "p"

        # Simulate an extraction that only produced one of the two members.
        real_extractall = zipfile.ZipFile.extractall

        def partial(self_zf, path=None, *a, **k):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "a.txt").write_text("a")

        zipfile.ZipFile.extractall = partial
        try:
            self.assertEqual(action.run(z), "fail")
        finally:
            zipfile.ZipFile.extractall = real_extractall

        self.assertTrue(z.exists(), "archive deleted after a partial extract")
        self.assertFalse((target / "b.txt").exists())

    def test_member_named_like_the_archive_is_refused(self):
        """mode=same: extracting it overwrites the archive, then unlink()
        deletes the extracted content too — an empty folder, reported ok."""
        z = self.inbox / "z.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("z.zip", "payload-that-would-eat-the-archive")

        self.assertEqual(UnzipAction(mode="same").run(z), "fail")
        self.assertTrue(z.exists(), "archive destroyed by its own member")
        self.assertNotEqual(z.read_bytes()[:4], b"payl")
        self.assertTrue(zipfile.is_zipfile(z), "archive replaced by the member")

        # mode=named puts it in a subfolder, so there is no collision.
        z2 = self.inbox / "ok.zip"
        with zipfile.ZipFile(z2, "w") as zf:
            zf.writestr("ok.zip", "inner")
        self.assertEqual(UnzipAction(mode="named").run(z2), "extracted")
        self.assertEqual((self.inbox / "ok" / "ok.zip").read_text(), "inner")

    def test_empty_archive_is_refused(self):
        z = self.inbox / "empty.zip"
        zipfile.ZipFile(z, "w").close()
        self.assertEqual(UnzipAction(mode="named").run(z), "fail")
        self.assertTrue(z.exists(), "empty archive deleted for doing nothing")

    def test_no_clobber_refuses_and_keeps_both(self):
        z = self.inbox / "p.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("f.txt", "from-zip")
        precious = self.inbox / "f.txt"
        precious.write_text("PRECIOUS")

        self.assertEqual(
            UnzipAction(mode="same", no_clobber=True).run(z), "fail"
        )
        self.assertEqual(precious.read_text(), "PRECIOUS")
        self.assertTrue(z.exists())

        # Default still overwrites, but says so.
        self.assertEqual(UnzipAction(mode="same").run(z), "extracted")
        self.assertEqual(precious.read_text(), "from-zip")

    def test_once_exit_code_reflects_failure(self):
        from folder_watch import main

        src = self.tmp / "src"
        src.mkdir()
        (src / "v.mp4").write_bytes(b"x" * 64)
        argv = [
            "copy", str(src), "--to", str(self.tmp / "gone" / "Out"),
            "--once", "--catch-up", "--stable", "1", "--min-bytes", "1",
            "--no-notify",
        ]
        self.assertEqual(main(argv), 1, "failed copy must not exit 0")

        # Nothing to do is not a failure.
        empty = self.tmp / "empty"
        empty.mkdir()
        ok = [
            "copy", str(empty), "--to", str(self.tmp / "dst"),
            "--once", "--stable", "1", "--min-bytes", "1", "--no-notify",
        ]
        self.assertEqual(main(ok), 0)

    def test_pidfiles_are_per_process(self):
        """Two watchers on one folder must not masquerade as one."""
        import os

        from folder_watch.support.pidfile import (
            collect_watch_status,
            pidfile_path,
            write_pidfile,
        )

        mine = write_pidfile("copy-src", {"name": "copy-src", "action": "copy"})
        self.assertIn(str(os.getpid()), mine.name)

        # A second, dead watcher under the same name.
        other = pidfile_path("copy-src", pid=999_999)
        other.write_text('{"pid": 999999, "name": "copy-src"}\n')
        try:
            rows = collect_watch_status(preset="copy-src")
            self.assertEqual(len(rows), 2, "one name collapsed two watchers")
            self.assertEqual(sum(1 for r in rows if r["alive"]), 1)
        finally:
            other.unlink(missing_ok=True)
            mine.unlink(missing_ok=True)


class TestLogSafetyAndPruneKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_filename_cannot_forge_a_log_line(self):
        import contextlib
        import io

        from folder_watch.support.log import emit

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            emit("progress", name="x\ncopied name=FAKE.mp4 size=999", size=1)
        out = buf.getvalue()
        self.assertNotIn("\ncopied name=FAKE.mp4", out)
        self.assertEqual(len(out.strip().splitlines()), 1, "emitted two lines")

    def test_plain_format_survives_spaces_and_quotes(self):
        from folder_watch.support.log import _plain

        self.assertEqual(_plain("simple.mp4"), "simple.mp4")
        self.assertEqual(_plain("with space.mp4"), '"with space.mp4"')
        self.assertEqual(_plain('quote".mp4'), '"quote\\".mp4"')
        self.assertEqual(_plain(123), "123")

    def test_json_mode_still_one_object_per_line(self):
        import contextlib
        import io
        import json as _json

        from folder_watch.support.log import emit, set_json_mode

        set_json_mode(True)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                emit("progress", name="x\ncopied name=FAKE.mp4", size=1)
            lines = buf.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(_json.loads(lines[0])["event"], "progress")
        finally:
            set_json_mode(False)

    def test_prune_keeps_entries_whose_path_contains_a_pipe(self):
        """Guessing produced a path that exists nowhere, so prune deleted it."""
        from folder_watch.support.state import DoneStore, file_identity

        weird = self.tmp / "dst|weird"
        weird.mkdir()
        src = self.tmp / "clip.mp4"
        src.write_bytes(b"x" * 32)

        store = DoneStore(self.tmp / "done.json")
        size, mtime_ns = file_identity(src)
        key = DoneStore.make_key(
            "copy", src, size=size, mtime_ns=mtime_ns, dest=weird / "clip.mp4"
        )
        store.mark(key, name=src.name, src=str(src))

        self.assertEqual(store.dead_keys(), [], "live entry classed as dead")
        self.assertEqual(store.prune(), 0)
        self.assertTrue(store.is_done(key))

        # An ambiguous key with no recorded src is left alone, not guessed at.
        self.assertIsNone(DoneStore.key_src("copy|/a|/b|c|d|1|2"))

        # And a genuinely dead entry is still collected.
        src.unlink()
        self.assertEqual(store.prune(), 1)


class TestRetryQueue(unittest.TestCase):
    """Transient failures survive an outage and a restart."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = self.tmp / "clip.mp4"
        self.f.write_bytes(b"x" * 64)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _q(self, **kw):
        from folder_watch.support.retry import RetryQueue

        return RetryQueue(self.tmp / "retry.json", **kw)

    def test_backoff_grows_and_is_not_due_early(self):
        from folder_watch.support.retry import BACKOFF

        q = self._q()
        t0 = 1_000_000.0
        e = q.schedule(
            self.f, action="copy", reason="t", size=64, mtime_ns=1, now=t0
        )
        self.assertEqual(e["attempts"], 1)
        self.assertEqual(e["next_due"], t0 + BACKOFF[0])
        self.assertEqual(q.due(now=t0), [])
        self.assertEqual(len(q.due(now=t0 + BACKOFF[0])), 1)

        e2 = q.schedule(
            self.f, action="copy", reason="t", size=64, mtime_ns=1, now=t0 + 100
        )
        self.assertEqual(e2["attempts"], 2)
        self.assertEqual(e2["next_due"], t0 + 100 + BACKOFF[1])
        self.assertEqual(len(q), 1, "same file queued twice")
        # first_failed is preserved, so the give-up budget runs from the
        # original failure rather than resetting on every attempt.
        self.assertEqual(e2["first_failed"], t0)

    def test_gives_up_after_the_budget(self):
        q = self._q(give_up_after=72 * 3600)
        t0 = 1_000_000.0
        q.schedule(self.f, action="copy", reason="t", size=64, mtime_ns=1, now=t0)

        # A weekend with the machine asleep: ~60h must NOT expire.
        self.assertEqual(q.expired(now=t0 + 60 * 3600), [])
        self.assertEqual(len(q), 1)

        gone = q.expired(now=t0 + 73 * 3600)
        self.assertEqual(len(gone), 1)
        self.assertEqual(len(q), 0)

    def test_survives_a_restart(self):
        t0 = 1_000_000.0
        q = self._q()
        q.schedule(self.f, action="copy", reason="t", size=64, mtime_ns=1, now=t0)
        reloaded = self._q()
        self.assertEqual(len(reloaded), 1)
        self.assertIn(str(self.f), reloaded.paths())

    def test_defer_does_not_count_an_attempt(self):
        """The destination short-circuit must not burn the give-up budget."""
        q = self._q()
        t0 = 1_000_000.0
        q.schedule(self.f, action="copy", reason="t", size=64, mtime_ns=1, now=t0)
        due = q.due(now=t0 + 999)
        self.assertEqual(len(due), 1)
        q.defer(due, now=t0 + 999)
        self.assertEqual(q.due(now=t0 + 999), [])
        self.assertEqual(due[0]["attempts"], 1, "defer counted as an attempt")

    def test_queue_is_bounded(self):
        q = self._q()
        for i in range(5):
            (self.tmp / f"f{i}.mp4").write_bytes(b"x")
        q.max_entries = 3
        added = [
            q.schedule(
                self.tmp / f"f{i}.mp4",
                action="copy", reason="t", size=1, mtime_ns=1, now=1.0,
            )
            for i in range(5)
        ]
        self.assertEqual(sum(1 for a in added if a is not None), 3)
        self.assertEqual(len(q), 3)

    def test_success_and_terminal_failure_both_clear_the_entry(self):
        from folder_watch.core.watch import apply_action

        dest = self.tmp / "dest"
        dest.mkdir()
        q = self._q()
        q.schedule(self.f, action="copy", reason="t", size=64, mtime_ns=1, now=1.0)
        self.assertEqual(len(q), 1)

        action = CopyAction(dest, notify_on_done=False)
        apply_action(self.f, action, state={}, stable_needed=1, retries=q)
        self.assertEqual(len(q), 0, "successful copy left a stale retry entry")


class TestLaunchAgentPlist(unittest.TestCase):
    def _plist(self, log: Path) -> str:
        from folder_watch.support.launchd import build_plist, stdio_log_path

        return build_plist(
            label="com.folder-watch.demo",
            preset="demo",
            python=Path("/x/.venv/bin/python"),
            root=Path("/x"),
            log_path=log,
            stdio_path=stdio_log_path(log),
            path_env="/usr/bin:/bin",
        )

    def test_stdio_never_shares_the_log_file(self):
        """Two writers on one file duplicate and overwrite each other mid-line."""
        from folder_watch.support.launchd import stdio_log_path

        for log in (Path("/s/a.log"), Path("/s/a"), Path("/s/a.b.log")):
            self.assertNotEqual(stdio_log_path(log), log)

        import xml.etree.ElementTree as ET

        log = Path("/s/demo.log")
        strings = [
            e.text for e in ET.fromstring(self._plist(log)).iter("string")
        ]
        self.assertIn(str(log), strings, "--log missing")
        self.assertIn("/s/demo.out.log", strings, "stdio path missing")
        self.assertEqual(strings.count(str(log)), 1, "log file has two writers")

    def test_plist_escapes_xml(self):
        """A path with & or < produced a plist launchctl refuses to load."""
        import xml.etree.ElementTree as ET

        log = Path("/s/Aziz & Co <v2>.log")
        root = ET.fromstring(self._plist(log))  # raises if malformed
        self.assertIn(str(log), [e.text for e in root.iter("string")])


class TestSettleNonBlocking(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_file_stays_waiting(self):
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

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
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

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
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

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
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

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
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

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
        import folder_watch.core.watch as watch_mod
        from folder_watch import settle_step

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
