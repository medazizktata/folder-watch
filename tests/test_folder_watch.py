#!/usr/bin/env python3
"""Tests for folder_watch core + actions."""

from __future__ import annotations

import contextlib
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

    def _notifications(self, action, zip_path):
        """Capture what the user would have been shown."""
        import folder_watch.actions.unzip as unzip_mod

        seen: list[tuple[str, str, Path | None]] = []
        old = unzip_mod.notify
        unzip_mod.notify = lambda t, b, **kw: seen.append((t, b, kw.get("reveal")))
        try:
            result = action.run(zip_path)
        finally:
            unzip_mod.notify = old
        return result, seen

    def test_a_successful_extract_is_announced(self):
        """copy has notified since July; unzip never did, so an inbox watcher
        ran in total silence."""
        z = self._make_zip("delivery.zip")
        action = UnzipAction(mode="named", notify_on_done=True)

        result, seen = self._notifications(action, z)

        self.assertEqual(result, "extracted")
        self.assertEqual(len(seen), 1)
        title, body, reveal = seen[0]
        self.assertIn("extracted", title)
        self.assertIn("delivery.zip", body)
        self.assertEqual(reveal, self.inbox / "delivery")

    def test_a_failed_extract_says_the_archive_was_kept(self):
        """A failure leaves the zip sitting there, so the folder looks exactly
        the same as if nothing had happened yet."""
        z = self.inbox / "broken.zip"
        z.write_bytes(b"not a zip at all")
        action = UnzipAction(mode="named", notify_on_done=True)

        result, seen = self._notifications(action, z)

        self.assertEqual(result, "fail")
        self.assertEqual(len(seen), 1)
        title, body, reveal = seen[0]
        self.assertIn("failed", title)
        self.assertIn("kept", body)
        self.assertEqual(reveal, z)
        self.assertTrue(z.is_file(), "the archive was not actually kept")

    def test_nothing_is_announced_when_notifications_are_off(self):
        z = self._make_zip("quiet.zip")
        action = UnzipAction(mode="named", notify_on_done=False)

        result, seen = self._notifications(action, z)

        self.assertEqual(result, "extracted")
        self.assertEqual(seen, [])

    def test_a_dry_run_announces_nothing(self):
        """Nothing happened, so there is nothing to tell the user about."""
        z = self._make_zip("preview.zip")
        action = UnzipAction(mode="named", notify_on_done=True, dry_run=True)

        result, seen = self._notifications(action, z)

        self.assertEqual(result, "would_extract")
        self.assertEqual(seen, [])

    def _corrupt_zip(self, name="broken.zip", members=2) -> Path:
        """A zip with an intact central directory and damaged local headers.

        The shape that actually turned up in production: is_zipfile() passes,
        the manifest lists every member with its size, and extraction dies on
        the first byte with "Bad magic number for file header". Vetting cannot
        catch it, because there is nothing wrong with what vetting reads.
        """
        z = self.inbox / name
        with zipfile.ZipFile(z, "w") as zf:
            for i in range(members):
                zf.writestr(f"member{i}.bin", b"payload" * 100)
        raw = bytearray(z.read_bytes())
        # Blank the local-header signatures; the central directory is untouched.
        for i in range(len(raw) - 4):
            if raw[i : i + 4] == b"PK\x03\x04":
                raw[i : i + 4] = b"XXXX"
        z.write_bytes(bytes(raw))
        return z

    def test_a_corrupt_archive_looks_valid_until_it_is_read(self):
        """Establishes the fixture matches production before relying on it."""
        z = self._corrupt_zip()

        self.assertTrue(zipfile.is_zipfile(z))
        with zipfile.ZipFile(z) as zf:
            self.assertEqual(len(zf.namelist()), 2)
            with self.assertRaises(zipfile.BadZipFile):
                zf.read("member0.bin")

    def test_a_failed_extract_leaves_no_directory_behind(self):
        """Production ran for weeks with an empty directory beside the archive.

        Vetting guarantees a *rejected* zip creates nothing; this one passes
        vetting and dies inside extractall, which the guarantee never covered.
        """
        z = self._corrupt_zip()
        target = self.inbox / "broken"
        action = UnzipAction(mode="named", notify_on_done=False)

        self.assertEqual(action.run(z), "fail")

        self.assertFalse(target.exists(), "stray directory left behind")
        self.assertTrue(z.is_file(), "the archive should be kept")

    def test_an_existing_directory_is_never_removed(self):
        """Only ever the directory this run created — anything already there
        belongs to someone else."""
        z = self._corrupt_zip()
        target = self.inbox / "broken"
        target.mkdir()
        (target / "someone-elses.txt").write_bytes(b"keep me")
        action = UnzipAction(mode="named", notify_on_done=False)

        self.assertEqual(action.run(z), "fail")

        self.assertTrue((target / "someone-elses.txt").is_file())

    def test_cleanup_cannot_remove_a_directory_with_anything_in_it(self):
        """The guarantee is rmdir's, not ours.

        A watched folder is somebody's live project. `created` is decided a
        moment before the directory is made, so a file landing in between —
        someone working in that folder, or a partial extract — would otherwise
        be inside something we believed was ours to delete. rmdir refuses a
        non-empty directory at the kernel, which no check here can race.
        """
        target = self.inbox / "half-done"
        target.mkdir()
        (target / "landed-anyway.mov").write_bytes(b"irreplaceable")

        UnzipAction._undo_target(target, self.inbox / "half-done.zip", True)

        self.assertTrue(target.is_dir(), "a non-empty directory was removed")
        self.assertEqual((target / "landed-anyway.mov").read_bytes(), b"irreplaceable")

    def test_cleanup_removes_only_an_empty_directory_it_made(self):
        target = self.inbox / "empty-one"
        target.mkdir()

        UnzipAction._undo_target(target, self.inbox / "empty-one.zip", True)

        self.assertFalse(target.exists())

    def test_cleanup_leaves_a_directory_it_did_not_create(self):
        target = self.inbox / "not-ours"
        target.mkdir()

        UnzipAction._undo_target(target, self.inbox / "not-ours.zip", False)

        self.assertTrue(target.is_dir())

    def test_a_partial_extraction_is_kept_and_reported_not_deleted(self):
        """Files the tool half-wrote are still files. Keeping them is safe now
        that output_exists reports the directory as incomplete rather than
        finished, so nothing downstream is misled by their presence."""
        z = self._make_zip("pack.zip")
        with zipfile.ZipFile(z, "a") as zf:
            zf.writestr("second.txt", b"more")
        target = self.inbox / "pack"
        target.mkdir()
        (target / "payload.txt").write_bytes(b"half of it")

        UnzipAction._undo_target(target, z, True)

        self.assertTrue((target / "payload.txt").is_file())
        self.assertIs(
            UnzipAction(mode="named", notify_on_done=False).output_exists(z), False
        )

    def test_mode_same_never_removes_the_watched_folder(self):
        """The target there is the archive's own folder. Removing it would
        take the inbox, the archive and everything else with it."""
        z = self._corrupt_zip()
        action = UnzipAction(mode="same", notify_on_done=False)

        self.assertEqual(action.run(z), "fail")

        self.assertTrue(self.inbox.is_dir(), "the watched folder was removed")
        self.assertTrue(z.is_file())

    def test_a_partial_extraction_does_not_read_as_finished(self):
        """--pending and baseline_pending are built on this answer, and it
        used to be "is there anything in the directory at all". One member of
        two hundred landing counted as a finished extract — so the run that
        most needs reporting was the one that got a false all-clear."""
        z = self._make_zip("pack.zip")
        with zipfile.ZipFile(z, "a") as zf:
            zf.writestr("second.txt", b"more")
        target = self.inbox / "pack"
        target.mkdir()
        (target / "payload.txt").write_bytes(b"hi")  # one of the two members

        action = UnzipAction(mode="named", notify_on_done=False)

        self.assertIs(action.output_exists(z), False)

    def test_a_complete_extraction_reads_as_finished(self):
        z = self._make_zip("pack.zip")
        target = self.inbox / "pack"
        target.mkdir()
        (target / "payload.txt").write_bytes(b"hi")

        action = UnzipAction(mode="named", notify_on_done=False)

        self.assertIs(action.output_exists(z), True)

    def test_a_corrupt_archive_admits_it_cannot_tell(self):
        """None, not False — a damaged manifest is not evidence of anything,
        and False would have --pending assert work was missed."""
        z = self.inbox / "garbage.zip"
        z.write_bytes(b"not a zip")

        self.assertIsNone(UnzipAction(mode="named").output_exists(z))

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

    def test_passthru_appends_verbatim_and_last(self):
        """The escape hatch: a flag ConvX grows tomorrow is usable today,
        without a release here first. Last, so it can override a mapped one."""
        from folder_watch import build_convx_watch_cmd

        cmd = build_convx_watch_cmd(
            self.src, format="webp", passthru="--new-flag 3 --other"
        )

        self.assertEqual(cmd[-3:], ["--new-flag", "3", "--other"])

    def test_passthru_quoting_survives(self):
        from folder_watch import build_convx_watch_cmd

        cmd = build_convx_watch_cmd(
            self.src, format="webp", passthru='--label "two words"'
        )

        self.assertEqual(cmd[-2:], ["--label", "two words"])

    def test_passthru_cannot_smuggle_a_second_command(self):
        """Split into argv and handed to Popen, never to a shell — so the
        metacharacters stay arguments to convx rather than becoming a command."""
        from folder_watch import build_convx_watch_cmd

        cmd = build_convx_watch_cmd(
            self.src, format="webp", passthru="--x; rm -rf /tmp/nope"
        )

        self.assertIn("--x;", cmd)
        self.assertNotIn("&&", cmd)
        # Every token is an argument to convx; nothing re-enters a shell.
        self.assertEqual(cmd[0].split("/")[-1], "convx")

    def test_passthru_is_absent_when_unset(self):
        from folder_watch import build_convx_watch_cmd

        base = build_convx_watch_cmd(self.src, format="webp")
        self.assertEqual(base[-1], "--overwrite")

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

    def test_no_builtin_presets(self):
        """Nothing is synthesised at import — presets come only from the file.

        A built-in used to be assembled from .env, which cannot work where the
        CLI has no .env to read, and was redundant anyway: the stored preset
        defines every field and is merged on top.
        """
        import json

        from folder_watch.presets import load_presets

        cfg_dir = self.tmp / "cfg"
        cfg_dir.mkdir()
        (cfg_dir / "presets.json").write_text(json.dumps({"only": {"action": "unzip"}}))
        import os

        prev = os.environ.get("FOLDER_WATCH_CONFIG_DIR")
        os.environ["FOLDER_WATCH_CONFIG_DIR"] = str(cfg_dir)
        try:
            import importlib

            import folder_watch.presets as presets_mod
            import folder_watch.support.paths as paths_mod

            importlib.reload(paths_mod)
            importlib.reload(presets_mod)
            self.assertEqual(sorted(presets_mod.load_presets()), ["only"])
            self.assertFalse(hasattr(presets_mod, "DEFAULT_PRESETS"))
        finally:
            if prev is None:
                os.environ.pop("FOLDER_WATCH_CONFIG_DIR", None)
            else:
                os.environ["FOLDER_WATCH_CONFIG_DIR"] = prev
            import importlib

            import folder_watch.presets as presets_mod
            import folder_watch.support.paths as paths_mod

            importlib.reload(paths_mod)
            importlib.reload(presets_mod)
        del load_presets

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


class TestArchiveDataLoss(unittest.TestCase):
    """Cases where extraction quietly kept less than the archive held."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_case_colliding_members_are_refused(self):
        """Three members, one file on APFS — every exists() check passed."""
        z = self.inbox / "docs.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Report.txt", "ONE")
            zf.writestr("report.txt", "TWO")
            zf.writestr("REPORT.TXT", "THREE")

        self.assertEqual(UnzipAction(mode="named").run(z), "fail")
        self.assertTrue(z.exists(), "archive deleted having kept a third of it")

    def test_unicode_normalisation_collision_is_refused(self):
        z = self.inbox / "u.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("café.txt", "NFC")       # é as one codepoint
            zf.writestr("café.txt", "NFD")      # e + combining acute
        self.assertEqual(UnzipAction(mode="named").run(z), "fail")
        self.assertTrue(z.exists())

    def test_ordinary_mixed_case_archive_still_extracts(self):
        """The check must not punish an archive that merely uses capitals."""
        z = self.inbox / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Report.txt", "ONE")
            zf.writestr("Notes.txt", "TWO")
        self.assertEqual(UnzipAction(mode="named").run(z), "extracted")
        self.assertEqual((self.inbox / "ok" / "Report.txt").read_text(), "ONE")
        self.assertEqual((self.inbox / "ok" / "Notes.txt").read_text(), "TWO")

    def test_mode_to_mirrors_the_tree(self):
        """Same stem in two subfolders shared one target, and both archives
        were then deleted — copy was fixed for this, unzip was not."""
        out = self.tmp / "out"
        out.mkdir()
        for client, payload in (("clientA", "A-FOOTAGE"), ("clientB", "B-FOOTAGE")):
            sub = self.inbox / client
            sub.mkdir()
            with zipfile.ZipFile(sub / "delivery.zip", "w") as zf:
                zf.writestr("video.mp4", payload)

        action = UnzipAction(mode="to", to=out, src_root=self.inbox)
        for client in ("clientA", "clientB"):
            zip_path = self.inbox / client / "delivery.zip"
            self.assertEqual(action.run(zip_path), "extracted")

        self.assertEqual(
            (out / "clientA" / "delivery" / "video.mp4").read_text(), "A-FOOTAGE"
        )
        self.assertEqual(
            (out / "clientB" / "delivery" / "video.mp4").read_text(), "B-FOOTAGE"
        )

    def test_verification_checks_size_not_just_presence(self):
        z = self.inbox / "p.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "full-length-content")

        real = zipfile.ZipFile.extractall

        def truncating(self_zf, path=None, *a, **k):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "a.txt").write_text("short")   # present, wrong size

        zipfile.ZipFile.extractall = truncating
        try:
            self.assertEqual(UnzipAction(mode="named").run(z), "fail")
        finally:
            zipfile.ZipFile.extractall = real
        self.assertTrue(z.exists(), "archive deleted after a truncated extract")


class TestAuditRoundThree(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_temp_sweep(self):
        """A killed copy left a full-size hidden file nothing ever removed."""
        import os

        from folder_watch.support.copy_io import sweep_stale_temps

        dest = self.dest / "clip.mp4"
        stale = self.dest / ".clip.mp4.abcd.tmp"
        fresh = self.dest / ".clip.mp4.efgh.tmp"
        unrelated = self.dest / ".other.mp4.ijkl.tmp"
        for f in (stale, fresh, unrelated):
            f.write_bytes(b"x" * 16)
        old = time.time() - 7200
        os.utime(stale, (old, old))
        os.utime(unrelated, (old, old))

        self.assertEqual(sweep_stale_temps(dest), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists(), "removed a copy that may be in flight")
        self.assertTrue(unrelated.exists(), "removed another file's temp")

    def test_once_reports_baseline_skips(self):
        """Skipping never-processed work while printing total success."""
        import contextlib
        import io

        from folder_watch.core.watch import process_file
        from folder_watch.support.state import DoneStore, file_identity

        f = self.src / "c.mp4"
        f.write_bytes(b"x" * 64)
        store = DoneStore(self.tmp / "done.json")
        action = CopyAction(self.dest, notify_on_done=False, src_root=self.src)
        size, mtime_ns = file_identity(f)
        key = store.mark_baseline_file(
            "copy", f, size=size, mtime_ns=mtime_ns, dest=action.done_dest(f)
        )
        self.assertTrue(store.is_baseline(key))

        state: dict = {}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = process_file(
                f,
                action,
                state=state,
                min_bytes=1,
                stable_needed=1,
                settle_interval=0.01,
                wait=False,
                done_store=store,
            )
        self.assertEqual(result, "pending")
        self.assertIn("baseline_no_output", buf.getvalue())

        # Second look says nothing — the poll backend rescans every cycle.
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            process_file(
                f,
                action,
                state=state,
                min_bytes=1,
                stable_needed=1,
                settle_interval=0.01,
                wait=False,
                done_store=store,
            )
        self.assertEqual(buf2.getvalue(), "", "repeated itself every scan")

    def test_verified_done_still_skips_quietly(self):
        """A genuine result must not be mistaken for an unmet assumption."""
        import contextlib
        import io

        from folder_watch.core.watch import process_file
        from folder_watch.support.state import DoneStore, file_identity

        f = self.src / "d.mp4"
        f.write_bytes(b"x" * 64)
        action = CopyAction(self.dest, notify_on_done=False, src_root=self.src)
        self.assertEqual(action.run(f), "copied")

        store = DoneStore(self.tmp / "done.json")
        size, mtime_ns = file_identity(f)
        key = DoneStore.make_key(
            "copy", f, size=size, mtime_ns=mtime_ns, dest=action.done_dest(f)
        )
        store.mark(key, name=f.name, result="copied", src=str(f))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = process_file(
                f,
                action,
                state={},
                min_bytes=1,
                stable_needed=1,
                settle_interval=0.01,
                wait=False,
                done_store=store,
            )
        self.assertEqual(result, "skip")
        self.assertIn("done_store", buf.getvalue())


class TestAuditRoundThreeTail(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dest_that_is_a_file_is_terminal(self):
        """A path that is a file will not become a directory by waiting."""
        from folder_watch.support.paths import (
            DestNotADirectory,
            DestUnavailable,
            ensure_dest_dir,
        )

        blocker = self.tmp / "notadir"
        blocker.write_text("i am a file")
        with self.assertRaises(DestNotADirectory):
            ensure_dest_dir(blocker)
        # Still a DestUnavailable, so existing handlers keep working.
        self.assertTrue(issubclass(DestNotADirectory, DestUnavailable))

        f = self.src / "x.mp4"
        f.write_bytes(b"x" * 64)
        action = CopyAction(blocker, notify_on_done=False)
        self.assertEqual(action.run(f), "fail", "queued a permanent failure")

    def test_missing_volume_is_still_transient(self):
        f = self.src / "x.mp4"
        f.write_bytes(b"x" * 64)
        action = CopyAction(self.tmp / "gone" / "Out", notify_on_done=False)
        self.assertEqual(action.run(f), "retry")

    def test_escape_covers_every_control_character(self):
        from folder_watch.support.log import _plain

        self.assertEqual(_plain("a\x1b[31mb"), "a\\x1b[31mb")
        self.assertEqual(_plain("a\x0bb"), "a\\x0bb")
        self.assertEqual(_plain("a\x7fb"), "a\\x7fb")
        self.assertNotIn("\x1b", _plain("\x1b]0;title\x07"))

    def test_directories_only_archive_is_refused(self):
        z = self.src / "dirs.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("folder/", "")
            zf.writestr("folder/nested/", "")
        self.assertEqual(UnzipAction(mode="named").run(z), "fail")
        self.assertTrue(z.exists(), "archive deleted for creating empty folders")

    def test_relative_and_absolute_dest_share_one_store(self):
        import os

        from folder_watch.support.state import DoneStore

        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            absolute = DoneStore.path_for(
                "copy", self.src, to=str(self.dest.resolve())
            )
            relative = DoneStore.path_for(
                "copy", self.src, to=str(Path("dest").expanduser().resolve())
            )
            self.assertEqual(absolute, relative)
        finally:
            os.chdir(cwd)


class TestDoneStoreWriteCost(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sole_writer_skips_the_merge_read(self):
        """Re-reading and re-parsing on every mark made a growing store
        quadratic; the merge is only needed when the file moved under us."""
        from folder_watch.support.state import DoneStore

        p = self.tmp / "d.json"
        store = DoneStore(p)
        store.mark("k1", name="a")
        self.assertIsNotNone(store._last_write)
        self.assertFalse(store._touched_by_someone_else())

        # Another process writes; the next save must notice and merge.
        other = DoneStore(p)
        other.mark("k2", name="b")
        self.assertTrue(store._touched_by_someone_else())
        store.mark("k3", name="c")
        self.assertEqual(sorted(DoneStore(p)._data), ["k1", "k2", "k3"])


class TestPresetForget(unittest.TestCase):
    """Removal is explicit, dry by default, and never yanks live config."""

    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_dir = self.tmp / "config"
        self.cfg_dir.mkdir()
        self.src = self.tmp / "src"
        self.src.mkdir()
        self._prev = os.environ.get("FOLDER_WATCH_CONFIG_DIR")
        os.environ["FOLDER_WATCH_CONFIG_DIR"] = str(self.cfg_dir)
        self._reload()

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("FOLDER_WATCH_CONFIG_DIR", None)
        else:
            os.environ["FOLDER_WATCH_CONFIG_DIR"] = self._prev
        self._reload()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reload(self):
        import importlib

        import folder_watch.cli as cli_mod
        import folder_watch.presets as presets_mod
        import folder_watch.support.paths as paths_mod

        importlib.reload(paths_mod)
        importlib.reload(presets_mod)
        importlib.reload(cli_mod)
        return cli_mod

    def _run(self, argv):
        import contextlib
        import io

        cli = self._reload()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def _make(self, name):
        self._run(["unzip", str(self.src), "--mode", "named", "--save-preset", name])

    def test_dry_by_default(self):
        self._make("doomed")
        code, out = self._run(["presets", "forget", "doomed"])
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertIn("doomed", (self.cfg_dir / "presets.json").read_text())

    def test_yes_removes_only_that_one(self):
        self._make("keep")
        self._make("doomed")
        code, _ = self._run(["presets", "forget", "doomed", "--yes"])
        self.assertEqual(code, 0)
        stored = (self.cfg_dir / "presets.json").read_text()
        self.assertNotIn("doomed", stored)
        self.assertIn("keep", stored)

    def test_unknown_name_exits_one_and_lists_known(self):
        self._make("keep")
        code, out = self._run(["presets", "forget", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("no preset named", out)
        self.assertIn("keep", out)

    def test_refuses_while_a_watcher_holds_it(self):
        import folder_watch.support.pidfile as pid_mod

        self._make("busy")
        real = pid_mod.live_watchers
        cli = self._reload()
        cli.live_watchers = lambda _n: [4242]
        try:
            code, out = self._run_no_reload(cli, ["presets", "forget", "busy", "--yes"])
            self.assertEqual(code, 2)
            self.assertIn("in use", out)
            self.assertIn("busy", (self.cfg_dir / "presets.json").read_text())
        finally:
            cli.live_watchers = real

    def _run_no_reload(self, cli, argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()


class TestPresetSaveMerges(unittest.TestCase):
    """Re-saving used to reset every field you didn't retype, and say 'saved'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_dir = self.tmp / "config"
        self.cfg_dir.mkdir()
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()
        import os

        self._prev = os.environ.get("FOLDER_WATCH_CONFIG_DIR")
        os.environ["FOLDER_WATCH_CONFIG_DIR"] = str(self.cfg_dir)
        self._reload()

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("FOLDER_WATCH_CONFIG_DIR", None)
        else:
            os.environ["FOLDER_WATCH_CONFIG_DIR"] = self._prev
        self._reload()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reload(self):
        import importlib

        import folder_watch.cli as cli_mod
        import folder_watch.presets as presets_mod
        import folder_watch.support.paths as paths_mod

        importlib.reload(paths_mod)
        importlib.reload(presets_mod)
        importlib.reload(cli_mod)
        return cli_mod

    def _stored(self, name="job"):
        import json

        return json.loads((self.cfg_dir / "presets.json").read_text())[name]

    def _save(self, argv):
        import contextlib
        import io

        cli = self._reload()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def _create(self):
        self._save([
            "copy", str(self.src), "--to", str(self.dest),
            "--ext", "mp4,mov", "--stable", "7", "--min-bytes", "500",
            "--recursive", "--no-clobber", "--retry-hours", "12",
            "--save-preset", "job",
        ])

    def test_changing_one_field_keeps_the_rest(self):
        self._create()
        self._save([
            "copy", str(self.src), "--to", str(self.dest),
            "--stable", "9", "--save-preset", "job",
        ])
        got = self._stored()
        self.assertEqual(got["stable"], 9)
        self.assertEqual(got["ext"], ["mp4", "mov"], "ext reset to the default")
        self.assertEqual(got["min_bytes"], 500, "min_bytes reset to the default")
        self.assertTrue(got["recursive"], "recursive silently turned off")
        self.assertTrue(got["no_clobber"], "safety flag silently dropped")
        self.assertEqual(got["retry_hours"], 12.0)

    def test_it_says_it_is_modifying_not_creating(self):
        self._create()
        out = self._save([
            "copy", str(self.src), "--to", str(self.dest),
            "--stable", "9", "--save-preset", "job",
        ])
        self.assertIn("already exists", out)
        self.assertIn("modifying", out)
        self.assertIn("stable: 7 -> 9", out)

    def test_no_op_save_reports_no_changes(self):
        self._create()
        out = self._save([
            "copy", str(self.src), "--to", str(self.dest),
            "--ext", "mp4,mov", "--stable", "7", "--min-bytes", "500",
            "--recursive", "--no-clobber", "--retry-hours", "12",
            "--save-preset", "job",
        ])
        self.assertIn("no changes", out)

    def test_creating_a_new_preset_captures_everything(self):
        out = self._save([
            "copy", str(self.src), "--to", str(self.dest),
            "--ext", "png", "--save-preset", "fresh",
        ])
        self.assertNotIn("already exists", out)
        got = self._stored("fresh")
        self.assertEqual(got["ext"], ["png"])
        # Defaults are captured on creation, which is what makes a preset useful.
        self.assertIn("backend", got)
        self.assertIn("settle", got)


class TestPresetStatus(unittest.TestCase):
    """A listing that renders a dead preset like a live one is the defect.

    Reporting is the whole fix: the tool cannot tell "this project is over"
    from "the drive is unplugged", so it says what is true and leaves the
    decision alone.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_healthy_preset_is_silent(self):
        from folder_watch.presets import preset_status

        level, note = preset_status(
            {"action": "unzip", "src": str(self.src), "mode": "named"}
        )
        self.assertEqual(level, "ok")
        self.assertEqual(note, "")

    def test_deleted_folder_names_the_nearest_existing_one(self):
        """Checking only the immediate parent called this an unmounted volume."""
        from folder_watch.presets import preset_status

        gone = self.tmp / "Project" / "Output"  # both levels missing
        level, note = preset_status({"action": "unzip", "src": str(gone)})
        self.assertEqual(level, "error")
        self.assertIn("missing", note)
        self.assertIn(str(self.tmp), note, "did not say where to look")
        self.assertNotIn("not mounted", note, "guessed the wrong cause")

    def test_unmounted_volume_reads_as_unmounted(self):
        from folder_watch.presets import preset_status

        level, note = preset_status(
            {"action": "unzip", "src": "/Volumes/DefinitelyNotMounted/x"}
        )
        self.assertEqual(level, "warn")
        self.assertIn("not mounted", note)

    def test_cloudstorage_counts_as_a_mount(self):
        """Must not depend on whether Google Drive is installed here.

        This passed locally and failed on a clean CI runner: the check walked
        up to the nearest EXISTING ancestor, so with Drive installed it found
        ~/Library/CloudStorage and said unmounted, and without it landed on
        ~/Library and said deleted. Same path, different answer per machine.
        """
        from folder_watch.presets import preset_status

        src = Path.home() / "Library/CloudStorage/GoogleDrive-nobody/Shared/x"
        level, note = preset_status({"action": "unzip", "src": str(src)})
        self.assertEqual(level, "warn")
        self.assertIn("not mounted", note)

    def test_mount_detection_ignores_whether_the_container_exists(self):
        import folder_watch.presets as presets_mod

        real = presets_mod._mount_containers
        presets_mod._mount_containers = lambda: (Path("/nowhere/Mounts"),)
        try:
            level, note = presets_mod.preset_status(
                {"action": "unzip", "src": "/nowhere/Mounts/Drive/x"}
            )
            self.assertEqual(level, "warn")
            self.assertIn("not mounted", note)
        finally:
            presets_mod._mount_containers = real

    def test_structurally_invalid_presets(self):
        from folder_watch.presets import preset_status

        self.assertEqual(
            preset_status({"action": "copy", "src": str(self.src)})[0], "error"
        )
        self.assertEqual(preset_status({"action": "nope", "src": "/tmp"})[0], "error")
        self.assertEqual(preset_status({"action": "copy"})[0], "error")

    def test_status_never_mutates_anything(self):
        from folder_watch.presets import preset_status

        gone = self.tmp / "Project" / "Output"
        before = sorted(p.name for p in self.tmp.rglob("*"))
        cfg = {"action": "unzip", "src": str(gone)}
        preset_status(cfg)
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)
        self.assertEqual(cfg, {"action": "unzip", "src": str(gone)}, "mutated the cfg")


class TestPresetCoverage(unittest.TestCase):
    """Nothing links argparse to the preset loader — so this does.

    A flag added without touching cli.py's preset block is silently not
    settable from a preset, which is how --no-clobber came to work perfectly
    on the command line and do nothing for the unattended watchers that
    needed it most. Adding a flag now forces a decision here.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_flag_is_wired_or_deliberately_excluded(self):
        from folder_watch.cli import build_parser
        from folder_watch.presets import NOT_PERSISTED, PRESET_KEYS

        unclassified = []
        for action in build_parser()._actions:
            if not action.option_strings:
                continue  # positional
            dest = action.dest
            if dest not in PRESET_KEYS and dest not in NOT_PERSISTED:
                unclassified.append(f"{action.option_strings[0]} (dest={dest})")

        self.assertEqual(
            unclassified,
            [],
            "new flag(s) neither preset-settable nor deliberately excluded — "
            "add the dest to PRESET_KEYS (and wire it in cli.py) or to "
            "NOT_PERSISTED:\n  " + "\n  ".join(unclassified),
        )

    def test_every_preset_key_is_actually_read_when_loading(self):
        """Classification is not wiring, and only one of them was checked.

        The sibling test asserts each flag is listed in PRESET_KEYS or
        NOT_PERSISTED — its own failure message even says "(and wire it in
        cli.py)" — but nothing confirmed that second half ever happened. A key
        could be added to PRESET_KEYS, turn the test green, and still be
        ignored at load time: exactly the --no-clobber bug the guard was
        written for, which worked on the command line and did nothing from a
        preset.

        So read the loader instead of trusting it, and ask which keys it
        actually takes off `cfg`.
        """
        import ast

        from folder_watch.presets import PRESET_KEYS

        source = (ROOT / "folder_watch" / "cli.py").read_text()
        block = self._preset_block(ast.parse(source))
        self.assertIsNotNone(block, "could not find the `if args.preset:` block")

        read = self._cfg_keys_read(block)
        # src and action are resolved by different names above the branch.
        unread = sorted(PRESET_KEYS - read - {"src", "action"})

        self.assertEqual(
            unread,
            [],
            "preset key(s) declared but never read when a preset is loaded — "
            "a preset setting them would be silently ignored: " + ", ".join(unread),
        )

    def test_the_loader_reads_nothing_it_has_not_declared(self):
        """The other direction: a key read but not in PRESET_KEYS works, yet
        load_presets warns "unknown key" about it every single run."""
        import ast

        from folder_watch.presets import PRESET_KEYS

        source = (ROOT / "folder_watch" / "cli.py").read_text()
        block = self._preset_block(ast.parse(source))
        undeclared = sorted(self._cfg_keys_read(block) - PRESET_KEYS)

        self.assertEqual(undeclared, [], f"read but not declared: {undeclared}")

    @staticmethod
    def _preset_block(tree):
        """The `if args.preset:` body in main(), where a preset is applied."""
        import ast

        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.Attribute)
                and test.attr == "preset"
                and isinstance(test.value, ast.Name)
                and test.value.id == "args"
            ):
                return node
        return None

    @staticmethod
    def _cfg_keys_read(block) -> set[str]:
        """Keys the block takes off `cfg`, however it spells the access."""
        import ast

        found: set[str] = set()
        for node in ast.walk(block):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "cfg"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "cfg"
                and isinstance(node.slice, ast.Constant)
            ):
                found.add(node.slice.value)
            # The ("--flag", "key") pairs the late-added flags loop over.
            elif (
                isinstance(node, ast.Tuple)
                and len(node.elts) == 2
                and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in node.elts
                )
                and node.elts[0].value.startswith("--")
            ):
                found.add(node.elts[1].value)
        return found

    def test_preset_values_reach_a_real_run(self):
        """End to end through the actual CLI, not a copy of its logic.

        The static guard above proves each key is read. This proves reading it
        has the effect it claims — every value here is deliberately different
        from the flag default, so a key that quietly fell through would report
        the default and fail.
        """
        import json
        import os
        import subprocess

        cfg_dir = Path(os.environ["FOLDER_WATCH_CONFIG_DIR"])
        presets_path = cfg_dir / "presets.json"
        before = presets_path.read_text() if presets_path.is_file() else None

        src = self.tmp / "watched"
        dest = self.tmp / "out"
        src.mkdir()
        dest.mkdir()
        wanted = {
            "action": "copy",
            "src": str(src),
            "to": str(dest),
            "ext": ["mov"],
            "backend": "poll",
            "debounce_ms": 750,
            "stable": 7,
            "settle": 1.25,
            "min_bytes": 4242,
            "recursive": True,
        }
        existing = json.loads(before) if before else {}
        existing["roundtrip-probe"] = wanted
        presets_path.write_text(json.dumps(existing))

        try:
            run = subprocess.run(
                [
                    sys.executable, "-m", "folder_watch",
                    "--preset", "roundtrip-probe", "--once", "--json",
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
        finally:
            if before is None:
                presets_path.unlink(missing_ok=True)
            else:
                presets_path.write_text(before)

        start = next(
            (
                json.loads(line)
                for line in run.stdout.splitlines()
                if line.startswith("{") and '"event": "start"' in line
            ),
            None,
        )
        self.assertIsNotNone(start, f"no start line:\n{run.stdout}\n{run.stderr}")

        self.assertEqual(Path(start["src"]), src.resolve())
        self.assertEqual(start["ext"], ["mov"])
        self.assertEqual(start["backend"], "poll")
        self.assertEqual(start["debounce_ms"], 750)
        self.assertEqual(start["stable"], 7)
        self.assertEqual(start["settle"], 1.25)
        self.assertEqual(start["min_bytes"], 4242)
        self.assertIs(start["recursive"], True)
        # `to` only shows up through the action's own description.
        self.assertIn(str(dest), start["action"])

    def test_no_stale_entries(self):
        """A key nothing can set is dead weight that still silences a warning."""
        from folder_watch.cli import build_parser
        from folder_watch.presets import NOT_PERSISTED, PRESET_KEYS

        dests = {a.dest for a in build_parser()._actions if a.option_strings}
        dests |= {"action", "src"}  # positionals
        both = (("PRESET_KEYS", PRESET_KEYS), ("NOT_PERSISTED", NOT_PERSISTED))
        for name, keys in both:
            stale = sorted(keys - dests)
            self.assertEqual(
                stale, [], f"{name} lists dest(s) no flag produces: {stale}"
            )

    def test_unknown_preset_key_warns_instead_of_passing_silently(self):
        import contextlib
        import io
        import json
        import os

        from folder_watch.presets import load_presets

        cfg_dir = self.tmp / "config"
        cfg_dir.mkdir()
        (cfg_dir / "presets.json").write_text(
            json.dumps(
                {
                    "demo": {
                        "action": "unzip",
                        "src": "/tmp",
                        "no_clober": True,       # typo
                        "dry_run": True,         # not persistable
                    }
                }
            )
        )
        prev = os.environ.get("FOLDER_WATCH_CONFIG_DIR")
        os.environ["FOLDER_WATCH_CONFIG_DIR"] = str(cfg_dir)
        try:
            import importlib

            import folder_watch.support.paths as paths_mod

            importlib.reload(paths_mod)
            import folder_watch.presets as presets_mod

            importlib.reload(presets_mod)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                presets_mod.load_presets()
            out = buf.getvalue()
            self.assertIn("unknown_preset_keys", out)
            self.assertIn("no_clober", out)
            self.assertIn("dry_run", out)
        finally:
            if prev is None:
                os.environ.pop("FOLDER_WATCH_CONFIG_DIR", None)
            else:
                os.environ["FOLDER_WATCH_CONFIG_DIR"] = prev
            import importlib

            import folder_watch.support.paths as paths_mod

            importlib.reload(paths_mod)
            import folder_watch.presets as presets_mod

            importlib.reload(presets_mod)
            del load_presets


class TestPresetCarriesSafetyFlags(unittest.TestCase):
    """Unattended runs use --preset, and presets could not carry these at all.

    So --no-clobber, --create-dest and friends were unreachable for exactly
    the runs that most need them: the ones nobody is watching.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, cfg: dict, argv: list[str]):
        """Resolve a preset the way main() does, without running a watcher.

        This mirrors cli.py rather than calling it, so on its own it would
        prove only that the copy agrees with itself. What makes it worth
        keeping is what comes after: build_action below is the real thing, so
        this covers args -> action. The two halves either side are covered
        elsewhere — TestPresetCoverage reads cli.py to prove every key is
        actually taken off cfg, and runs one preset end to end through the
        CLI. Do not grow this helper; grow that round-trip.
        """
        import argparse

        from folder_watch.cli import build_parser

        args = build_parser().parse_args(argv)
        passed = {a.split("=")[0] for a in argv}
        for flag, key in (
            ("--no-clobber", "no_clobber"),
            ("--create-dest", "create_dest"),
            ("--exclusive", "exclusive"),
            ("--no-done-store", "no_done_store"),
        ):
            if flag not in passed and cfg.get(key):
                setattr(args, key, True)
        if "--retry-hours" not in passed and cfg.get("retry_hours") is not None:
            args.retry_hours = float(cfg["retry_hours"])
        self.assertIsInstance(args, argparse.Namespace)
        return args

    def test_preset_supplies_the_flag(self):
        args = self._args({"no_clobber": True}, ["unzip", str(self.tmp)])
        self.assertTrue(args.no_clobber)
        built = self._args({"create_dest": True, "retry_hours": 12}, ["copy", "x"])
        self.assertTrue(built.create_dest)
        self.assertEqual(built.retry_hours, 12.0)

    def test_command_line_still_wins(self):
        """An explicit flag must not be quietly overridden by the preset."""
        args = self._args({}, ["unzip", str(self.tmp), "--no-clobber"])
        self.assertTrue(args.no_clobber)

    def test_flag_reaches_the_action(self):
        from folder_watch.cli import build_action

        # --mode same, so the target IS this folder and a collision is real.
        # Under the default --mode named the target is a subfolder and there
        # is nothing to clobber, which is correct but tests nothing.
        args = self._args(
            {"no_clobber": True}, ["unzip", str(self.tmp), "--mode", "same"]
        )
        action = build_action("unzip", args, src_root=self.tmp)
        self.assertTrue(action.no_clobber, "preset flag never reached the action")

        z = self.tmp / "p.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("f.txt", "from-zip")
        (self.tmp / "f.txt").write_text("PRECIOUS")
        self.assertEqual(action.run(z), "fail")
        self.assertEqual((self.tmp / "f.txt").read_text(), "PRECIOUS")
        self.assertTrue(z.exists())


class TestRestrictedEnvironment(unittest.TestCase):
    """The environment the tool actually runs in, not the one we develop in.

    A watcher started by launchd gets a bare PATH, no interactive shell and a
    different HOME. `lsof` lives in /usr/sbin, which launchd's default PATH
    omits — and a version of this treated "lsof missing" as "file still open",
    so nothing ever settled and both production watchers silently processed
    nothing while logging progress forever.

    Three rounds of independent audit missed it: every evaluator ran with a
    normal shell PATH where lsof resolves. These tests run the real code in
    the real restricted environment so that class of bug fails here instead.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restricted(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        from folder_watch.support.launchd import launchagent_path_env

        env = {
            "PATH": launchagent_path_env(),
            "HOME": str(self.tmp),
            "PYTHONPATH": str(ROOT),
        }
        env.update(extra or {})
        return env

    def _run(self, code: str, extra: dict[str, str] | None = None):
        import subprocess

        return subprocess.run(
            [sys.executable, "-c", code],
            env=self._restricted(extra),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_lsof_is_findable_under_the_launchagent_path(self):
        r = self._run(
            "from folder_watch.core.util import lsof_binary; print(lsof_binary())"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(
            r.stdout.strip(),
            "None",
            "lsof unfindable under the PATH the installer writes",
        )

    def test_closed_file_does_not_look_open_under_that_path(self):
        """The exact failure: every file appearing permanently open."""
        target = self.tmp / "closed.bin"
        target.write_bytes(b"x" * 32)
        r = self._run(
            "from pathlib import Path\n"
            "from folder_watch.core.util import is_open\n"
            f"print(is_open(Path({str(target)!r})))"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.strip(), "0", "a closed file looked open — nothing would settle"
        )

    def test_a_copy_completes_end_to_end_under_that_environment(self):
        """Not just is_open: the whole settle path must actually finish."""
        src = self.tmp / "src"
        dest = self.tmp / "dest"
        src.mkdir()
        dest.mkdir()
        (src / "clip.mp4").write_bytes(b"x" * 4096)

        r = self._run(
            "from folder_watch.cli import main\n"
            "raise SystemExit(main(["
            f"'copy', {str(src)!r}, '--to', {str(dest)!r}, '--once', '--catch-up',"
            "'--ext', 'mp4', '--min-bytes', '1', '--stable', '1', '--no-notify']))"
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(
            (dest / "clip.mp4").is_file(),
            f"copy never completed under the LaunchAgent environment:\n{r.stdout}",
        )

    def test_installer_path_actually_contains_lsof(self):
        """Guards the PATH itself: dropping /usr/sbin must fail here."""
        import os

        from folder_watch.support.launchd import launchagent_path_env

        found = [
            d
            for d in launchagent_path_env().split(":")
            if os.access(os.path.join(d, "lsof"), os.X_OK)
        ]
        self.assertTrue(
            found, "no directory in the LaunchAgent PATH contains an executable lsof"
        )

    def test_missing_lsof_degrades_instead_of_deadlocking(self):
        """If lsof genuinely isn't there, settle on size — never 'open forever'."""
        empty_bin = self.tmp / "nobin"
        empty_bin.mkdir()
        target = self.tmp / "f.bin"
        target.write_bytes(b"x" * 32)
        r = self._run(
            "from pathlib import Path\n"
            "from folder_watch.core.util import is_open, lsof_binary\n"
            "print('binary', lsof_binary())\n"
            f"print('open', is_open(Path({str(target)!r})))",
            extra={"PATH": str(empty_bin)},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        # Only meaningful if lsof really is unreachable in this run.
        if "binary None" in r.stdout:
            self.assertIn(
                "open 0", r.stdout, "unreachable lsof reported every file as open"
            )


class TestConvxChildReaping(unittest.TestCase):
    """`convx watch` never exits on its own — it must not outlive its wrapper."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_child_dies_when_the_wrapper_is_terminated(self):
        import os
        import signal
        import subprocess
        import sys

        from folder_watch.actions.convx import run_convx_watch

        marker = self.tmp / "child.pid"
        # Stand-in for `convx watch`: records its pid, then never exits.
        child_code = (
            f"import os,time;open({str(marker)!r},'w').write(str(os.getpid()));"
            "time.sleep(300)"
        )

        # A subprocess, not os.fork(). Forking a multi-threaded process is
        # unsafe — Python warns about it now and is moving toward refusing —
        # and the test runner has threads. This delivers the same SIGTERM that
        # launchd would, from outside.
        killer_code = (
            f"import os,signal,time; time.sleep(0.5); "
            f"os.kill({os.getpid()}, signal.SIGTERM)"
        )

        # Guard the runner itself: run_convx_watch installs its own SIGTERM
        # handler and restores THIS one when it returns, so a signal arriving
        # late cannot kill the test process.
        caught: list[int] = []
        prev = signal.signal(signal.SIGTERM, lambda *_a: caught.append(1))
        killer = subprocess.Popen([sys.executable, "-c", killer_code])
        try:
            rc = run_convx_watch([sys.executable, "-c", child_code])
        finally:
            signal.signal(signal.SIGTERM, prev)
            try:
                killer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                killer.kill()
        self.assertEqual(rc, 0)

        self.assertTrue(marker.is_file(), "stand-in child never started")
        pid = int(marker.read_text())
        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except OSError:
                alive = False
                break
        if alive:  # don't leak it out of the test suite
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            subprocess.run(["true"], check=False)
        self.assertFalse(alive, "child outlived the wrapper")


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


class TestServiceInterpreter(unittest.TestCase):
    """Which interpreter a LaunchAgent is pointed at, and what is on its path.

    Getting this wrong does not fail loudly at install time — the plist is
    valid, launchd accepts it, and the service then crash-loops on "No module
    named folder_watch" with KeepAlive dutifully restarting it. Which is how a
    live watcher went down for several minutes.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_venv(self, name="venv") -> Path:
        """A venv shaped like a real one, symlinked python included.

        The symlink is the point. A venv's bin/python points at the base
        interpreter, so anything that resolve()s it walks out of the venv and
        loses the site-packages the whole install lives in.
        """
        base = self.tmp / "base-python"
        base.write_text("#!/bin/sh\n")
        venv = self.tmp / name
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /somewhere\n")
        (venv / "bin" / "python").symlink_to(base)
        (venv / "bin" / "folder-watch").write_text("#!/bin/sh\n")
        return venv

    def test_an_installed_copy_keeps_its_own_interpreter(self):
        from folder_watch.support.launchd import _resolve_python_and_root

        venv = self._fake_venv()

        python, root = _resolve_python_and_root(venv / "bin" / "python")

        self.assertEqual(python, venv / "bin" / "python")
        self.assertIsNone(root, "an installed copy must not be given a root")

    def test_the_console_script_resolves_to_the_same_interpreter(self):
        from folder_watch.support.launchd import _resolve_python_and_root

        venv = self._fake_venv()

        python, root = _resolve_python_and_root(venv / "bin" / "folder-watch")

        self.assertEqual(python, venv / "bin" / "python")
        self.assertIsNone(root)

    def test_an_installed_copy_gets_no_pythonpath(self):
        """The one that matters. A PYTHONPATH naming a checkout puts that
        checkout ahead of site-packages, so the service silently goes back to
        running the working tree — the exact coupling an installed copy exists
        to remove."""
        from folder_watch.support.launchd import build_plist, stdio_log_path

        body = build_plist(
            label="com.folder-watch.demo",
            preset="demo",
            python=self.tmp / "venv" / "bin" / "python",
            root=None,
            log_path=self.tmp / "demo.log",
            stdio_path=stdio_log_path(self.tmp / "demo.log"),
            path_env="/usr/bin:/bin:/usr/sbin",
        )

        import xml.etree.ElementTree as ET

        self.assertNotIn("PYTHONPATH", body)
        self.assertIn("/usr/sbin", body, "lsof must stay reachable")
        ET.fromstring(body)  # still valid XML

    def test_a_checkout_still_gets_its_root_on_the_path(self):
        """The other layout has to keep working: run from a clone with a
        .venv, the package is not installed and PYTHONPATH is what finds it."""
        from folder_watch.support.launchd import (
            _resolve_python_and_root,
            build_plist,
            stdio_log_path,
        )

        root = self.tmp / "checkout"
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (root / "bin").mkdir()
        (root / "bin" / "folder-watch").write_text("#!/bin/sh\n")

        python, resolved_root = _resolve_python_and_root(root / "bin" / "folder-watch")

        self.assertEqual(python, root / ".venv" / "bin" / "python")
        self.assertEqual(resolved_root, root)

        body = build_plist(
            label="com.folder-watch.demo",
            preset="demo",
            python=python,
            root=resolved_root,
            log_path=self.tmp / "demo.log",
            stdio_path=stdio_log_path(self.tmp / "demo.log"),
            path_env="/usr/bin:/bin",
        )
        import xml.etree.ElementTree as ET

        self.assertIn("PYTHONPATH", body)
        self.assertIn(str(root), body)
        ET.fromstring(body)


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

        log = Path("/s/Acme & Co <v2>.log")
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
        """An aborted export: 0 bytes forever must not be watched forever."""
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
