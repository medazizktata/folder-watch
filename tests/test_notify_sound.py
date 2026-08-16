#!/usr/bin/env python3
"""Tests for the notification sound.

Nothing here plays anything. Each notifier is replaced with a recorder, so
what is checked is the argv handed to it — which is the whole of what this
feature does, and the part that can be wrong without anyone noticing: an
unrecognised sound name is accepted by every backend and simply plays
nothing, so a typo is indistinguishable from the feature being switched off.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)
from _loopharness import captured, fields

import folder_watch.support.notify as notify_mod
from folder_watch.support.notify import (
    DEFAULT_SOUND,
    available_sounds,
    resolve_sound,
)


class Recorder:
    """Stands in for a notifier binary; records argv instead of running it."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[list[str]] = []

    def run(self, cmd, *a, **kw):
        self.calls.append([str(c) for c in cmd])

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    def which(self, binary: str):
        return f"/fake/{binary}" if binary == self.name else None

    def flag_after(self, flag: str) -> str | None:
        for cmd in self.calls:
            if flag in cmd:
                return cmd[cmd.index(flag) + 1]
        return None


class SoundCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._real_run = notify_mod.subprocess.run
        self._real_which = notify_mod.shutil.which
        self._real_thread = notify_mod.threading.Thread
        # Run the notifier body inline; a background thread would let the test
        # assert before it had done anything.
        notify_mod.threading.Thread = lambda target, **kw: type(
            "Now", (), {"start": staticmethod(target)}
        )()
        notify_mod._warned_missing.clear()

    def tearDown(self):
        notify_mod.subprocess.run = self._real_run
        notify_mod.shutil.which = self._real_which
        notify_mod.threading.Thread = self._real_thread
        notify_mod._warned_missing.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def with_backend(self, name: str) -> Recorder:
        rec = Recorder(name)
        notify_mod.subprocess.run = rec.run
        notify_mod.shutil.which = rec.which
        return rec


class TestResolving(unittest.TestCase):
    def setUp(self):
        notify_mod._warned_missing.clear()

    def tearDown(self):
        notify_mod._warned_missing.clear()

    def test_the_default_exists_on_this_machine(self):
        """A default that resolves to nothing is a feature that silently does
        nothing, which is the failure this whole module is guarding against."""
        self.assertIn(DEFAULT_SOUND, available_sounds())
        self.assertEqual(resolve_sound(DEFAULT_SOUND), DEFAULT_SOUND)

    def test_the_default_is_one_of_the_quiet_ones(self):
        """The request was subtle, not annoying. These are the macOS sounds
        that announce themselves — an error buzz or a several-second motif is
        the wrong thing to hear once per finished file."""
        self.assertNotIn(
            DEFAULT_SOUND,
            {"Basso", "Sosumi", "Funk", "Submarine", "Hero", "Glass", "Ping"},
        )

    def test_every_offered_name_actually_resolves(self):
        for name in available_sounds():
            with self.subTest(sound=name):
                self.assertEqual(resolve_sound(name), name)

    def test_an_unknown_name_is_reported_rather_than_silently_ignored(self):
        with captured() as log:
            self.assertIsNone(resolve_sound("NoSuchSound"))

        warned = fields(log, "warn")
        self.assertEqual(warned[0]["reason"], "unknown_sound")
        self.assertEqual(warned[0]["sound"], "NoSuchSound")

    def test_the_complaint_is_made_once_not_once_per_file(self):
        """It would otherwise repeat for every file in a batch, which is the
        log-spam shape this project has fixed twice already."""
        with captured() as log:
            for _ in range(5):
                resolve_sound("NoSuchSound")

        self.assertEqual(len(fields(log, "warn")), 1)

    def test_switching_it_off_is_not_an_error(self):
        with captured() as log:
            self.assertIsNone(resolve_sound(None))
            self.assertIsNone(resolve_sound(""))
            self.assertIsNone(resolve_sound("none"))

        self.assertEqual(fields(log, "warn"), [])


class TestItReachesTheNotifier(SoundCase):
    def test_alerter_is_given_the_sound(self):
        rec = self.with_backend("alerter")

        notify_mod.notify("t", "b", reveal=self.tmp, sound=DEFAULT_SOUND)

        self.assertEqual(rec.flag_after("-sound"), DEFAULT_SOUND)

    def test_terminal_notifier_is_given_the_sound(self):
        rec = self.with_backend("terminal-notifier")

        notify_mod.notify("t", "b", reveal=self.tmp, sound=DEFAULT_SOUND)

        self.assertEqual(rec.flag_after("-sound"), DEFAULT_SOUND)

    def test_the_osascript_fallback_is_given_the_sound(self):
        rec = self.with_backend("nothing-installed")

        notify_mod.notify("t", "b", sound=DEFAULT_SOUND)

        script = " ".join(rec.calls[0])
        self.assertIn(f'sound name "{DEFAULT_SOUND}"', script)

    def test_no_sound_means_no_flag_at_all(self):
        """Not an empty value — every backend treats a bare flag differently,
        and the absence is what "off" has to mean."""
        for backend in ("alerter", "terminal-notifier", "nothing-installed"):
            with self.subTest(backend=backend):
                rec = self.with_backend(backend)
                notify_mod.notify("t", "b", reveal=self.tmp, sound=None)
                joined = " ".join(" ".join(c) for c in rec.calls)
                self.assertNotIn("-sound", joined)
                self.assertNotIn("sound name", joined)

    def test_an_unknown_sound_still_sends_the_notification(self):
        """Degrade, do not fail: a bad sound name must not cost you the
        notification itself."""
        rec = self.with_backend("alerter")

        with captured():
            notify_mod.notify("t", "b", reveal=self.tmp, sound="NoSuchSound")

        self.assertEqual(len(rec.calls), 1, "the notification was lost")
        self.assertNotIn("-sound", " ".join(rec.calls[0]))

    def test_a_name_that_is_not_a_real_sound_never_reaches_the_notifier(self):
        """The first defence, and the one that does the work: a sound name has
        to match a file on disk, so it can never be arbitrary text from a
        preset someone edited."""
        rec = self.with_backend("nothing-installed")

        with captured():
            notify_mod.notify("t", "b", sound='X" & do shell script "id')

        self.assertNotIn("do shell script", " ".join(" ".join(c) for c in rec.calls))

    def test_a_hostile_filename_is_escaped_on_the_way_into_applescript(self):
        """The second defence, tested where it actually applies.

        Asserting the above with a name that does not resolve proves nothing —
        it would pass with the escaping deleted. macOS filenames may contain a
        double quote, so a sound really can be named this, and only then does
        the escaping matter.
        """
        sounds = self.tmp / "Sounds"
        sounds.mkdir()
        hostile = 'X" & do shell script "id'
        (sounds / f"{hostile}.aiff").write_bytes(b"")

        rec = self.with_backend("nothing-installed")
        real_dirs = notify_mod.SOUND_DIRS
        notify_mod.SOUND_DIRS = (sounds,)
        try:
            with captured():
                # It resolves now — the file exists — so it does reach the script.
                self.assertEqual(resolve_sound(hostile), hostile)
                notify_mod.notify("t", "b", sound=hostile)
        finally:
            notify_mod.SOUND_DIRS = real_dirs

        script = " ".join(" ".join(c) for c in rec.calls)
        self.assertIn("sound name", script, "the sound was not passed at all")
        self.assertNotIn('" & do shell script "', script, "quote broke out")
        self.assertIn('\\"', script, "the quote was not escaped")


class TestActionsPassItThrough(SoundCase):
    """Every action that notifies has to carry the setting, or turning it on
    works for copy and does nothing for the folder you actually watch."""

    def test_each_notifying_action_accepts_and_forwards_a_sound(self):
        from folder_watch.actions.copy import CopyAction
        from folder_watch.actions.exec_cmd import ExecAction
        from folder_watch.actions.unzip import UnzipAction

        cases = (
            CopyAction(self.tmp, sound="Pop"),
            UnzipAction(mode="named", sound="Pop"),
            ExecAction("tool {path}", sound="Pop"),
        )
        for action in cases:
            with self.subTest(action=action.name):
                self.assertEqual(action.sound, "Pop")

    def test_the_cli_turns_the_flags_into_a_sound(self):
        import argparse

        from folder_watch.cli import build_parser, chosen_sound

        parser = build_parser()

        self.assertEqual(chosen_sound(parser.parse_args([])), DEFAULT_SOUND)
        self.assertIsNone(chosen_sound(parser.parse_args(["--no-sound"])))
        self.assertEqual(
            chosen_sound(parser.parse_args(["--sound-name", "Pop"])), "Pop"
        )
        # Off wins over a name, so --no-sound is not quietly overridden.
        self.assertIsNone(
            chosen_sound(parser.parse_args(["--no-sound", "--sound-name", "Pop"]))
        )
        self.assertIsInstance(parser.parse_args([]), argparse.Namespace)


if __name__ == "__main__":
    unittest.main()
