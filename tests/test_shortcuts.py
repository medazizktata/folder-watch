#!/usr/bin/env python3
"""Tests for the watch-copy / watch-unzip / watch-convx argv shims.

These have only ever been exercised end to end, through a subprocess, which
proves they run but says nothing about what argv they build. The one bug they
have had was exactly that: a flag missing from the value-flag set had its value
read as a positional, and the shim rejected a perfectly good command line.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import shortcuts


class ShimCase(unittest.TestCase):
    """Captures the argv the shim hands to folder-watch, without running it."""

    def setUp(self):
        self.seen: list[list[str]] = []
        self.original = shortcuts.fw_main
        shortcuts.fw_main = lambda argv: self.seen.append(list(argv)) or 0

    def tearDown(self):
        shortcuts.fw_main = self.original

    def run_shim(self, fn, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(argv)
        return rc, out.getvalue(), err.getvalue()

    def built(self) -> list[str]:
        self.assertEqual(len(self.seen), 1, "folder-watch was not called once")
        return self.seen[0]


class TestCopyShim(ShimCase):
    def test_two_positionals_become_a_source_and_a_destination(self):
        rc, _out, _err = self.run_shim(shortcuts.copy_main, ["./Out", "./Drive"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["copy", "./Out", "--to", "./Drive"])

    def test_a_flag_value_is_not_mistaken_for_a_positional(self):
        """Every folder-watch flag that takes a value has to be listed. One
        that is missing has its value read as a third positional, and the shim
        rejects the command with "unexpected"."""
        rc, _out, _err = self.run_shim(
            shortcuts.copy_main,
            ["./Out", "./Drive", "--log", "run.log", "--ext", "mp4", "--stable", "3"],
        )

        self.assertEqual(rc, 0)
        self.assertEqual(
            self.built(),
            [
                "copy", "./Out", "--to", "./Drive",
                "--log", "run.log", "--ext", "mp4", "--stable", "3",
            ],
        )

    def test_no_flag_value_is_ever_read_as_a_positional(self):
        """The property itself, over every flag the parser has.

        This used to assert that a hand-written frozenset agreed with the
        parser, which caught drift but still left someone to go and fix it —
        and it only ever compared lists, never behaviour. The shim now derives
        the set from the parser, so the thing worth checking is the outcome:
        whatever the flag, the two positionals still arrive as two positionals.
        """
        for flag in sorted(shortcuts.VALUE_FLAGS):
            if flag == "--preset":
                continue  # handled earlier as a passthrough; never split
            with self.subTest(flag=flag):
                self.seen.clear()
                rc, _out, err = self.run_shim(
                    shortcuts.copy_main, ["./Out", "./Drive", flag, "VALUE"]
                )
                self.assertEqual(rc, 0, f"{flag} broke the split: {err}")
                built = self.built()
                self.assertEqual(built[:4], ["copy", "./Out", "--to", "./Drive"])
                self.assertEqual(built[-2:], [flag, "VALUE"])

    def test_a_boolean_flag_does_not_swallow_the_next_word(self):
        """The mirror image: nargs=0 flags must not eat a positional."""
        from folder_watch.cli import build_parser

        booleans = sorted(
            {
                opt
                for action in build_parser()._actions
                for opt in action.option_strings
                if action.nargs == 0
                and opt not in ("-h", "--help", "--version")
                # These hand the whole argv over untouched by design, so there
                # is no rearrangement to check.
                and opt not in shortcuts.PASSTHROUGH
            }
        )
        self.assertTrue(booleans, "no boolean flags found — the probe is wrong")

        for flag in booleans:
            with self.subTest(flag=flag):
                self.seen.clear()
                rc, _out, _err = self.run_shim(
                    shortcuts.copy_main, [flag, "./Out", "./Drive"]
                )
                self.assertEqual(rc, 0)
                self.assertEqual(
                    self.built()[:4], ["copy", "./Out", "--to", "./Drive"]
                )

    def test_the_value_flags_come_from_the_parser(self):
        """Derived, not remembered — so there is no second list to drift."""
        from folder_watch.cli import build_parser

        expected = {
            opt
            for action in build_parser()._actions
            for opt in action.option_strings
            if action.nargs != 0
        }

        self.assertEqual(set(shortcuts.VALUE_FLAGS), expected)

    def test_a_valueless_form_needs_no_help(self):
        """argparse splits --flag=value itself."""
        rc, _out, _err = self.run_shim(
            shortcuts.copy_main, ["./Out", "./Drive", "--ext=mp4"]
        )

        self.assertEqual(rc, 0)
        self.assertEqual(
            self.built(), ["copy", "./Out", "--to", "./Drive", "--ext=mp4"]
        )

    def test_a_missing_destination_is_refused_with_a_usage_line(self):
        rc, _out, err = self.run_shim(shortcuts.copy_main, ["./Out"])

        self.assertEqual(rc, 2)
        self.assertIn("usage: watch-copy SRC DEST", err)
        self.assertEqual(self.seen, [])

    def test_a_third_positional_is_named_rather_than_ignored(self):
        rc, _out, err = self.run_shim(
            shortcuts.copy_main, ["./Out", "./Drive", "./Extra"]
        )

        self.assertEqual(rc, 2)
        self.assertIn("./Extra", err)
        self.assertEqual(self.seen, [])

    def test_help_explains_the_rearrangement_and_runs_nothing(self):
        rc, out, _err = self.run_shim(shortcuts.copy_main, ["--help"])

        self.assertEqual(rc, 0)
        self.assertIn("folder-watch copy SRC --to DEST", out)
        self.assertEqual(self.seen, [])

    def test_a_preset_is_handed_over_untouched(self):
        """A preset already carries its source and destination, so there is
        nothing to rearrange."""
        rc, _out, _err = self.run_shim(
            shortcuts.copy_main, ["--preset", "nightly", "-v"]
        )

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["--preset", "nightly", "-v"])

    def test_the_joined_preset_form_is_recognised_too(self):
        rc, _out, _err = self.run_shim(shortcuts.copy_main, ["--preset=nightly"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["--preset=nightly"])

    def test_listing_flags_are_handed_over_untouched(self):
        for flag in ("--list-presets", "--list-actions"):
            with self.subTest(flag=flag):
                self.seen.clear()
                rc, _out, _err = self.run_shim(shortcuts.copy_main, [flag])
                self.assertEqual(rc, 0)
                self.assertEqual(self.built(), [flag])


class TestUnzipShim(ShimCase):
    def test_one_positional_is_the_source(self):
        rc, _out, _err = self.run_shim(
            shortcuts.unzip_main, ["./inbox", "--mode", "same"]
        )

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["unzip", "./inbox", "--mode", "same"])

    def test_no_source_and_two_sources_are_both_refused(self):
        for argv in ([], ["./a", "./b"]):
            with self.subTest(argv=argv):
                self.seen.clear()
                rc, _out, err = self.run_shim(shortcuts.unzip_main, argv)
                self.assertEqual(rc, 2)
                self.assertIn("usage: watch-unzip SRC", err)
                self.assertEqual(self.seen, [])

    def test_help_runs_nothing(self):
        rc, out, _err = self.run_shim(shortcuts.unzip_main, ["-h"])

        self.assertEqual(rc, 0)
        self.assertIn("folder-watch unzip SRC", out)
        self.assertEqual(self.seen, [])

    def test_a_preset_is_handed_over_untouched(self):
        rc, _out, _err = self.run_shim(shortcuts.unzip_main, ["--preset", "inbox"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["--preset", "inbox"])


class TestConvxShim(ShimCase):
    def test_the_conversion_flags_keep_their_values(self):
        rc, _out, _err = self.run_shim(
            shortcuts.convx_main,
            ["./Inbox", "--format", "webp", "--quality", "80", "--ext", "png,jpg"],
        )

        self.assertEqual(rc, 0)
        self.assertEqual(
            self.built(),
            [
                "convx", "./Inbox",
                "--format", "webp", "--quality", "80", "--ext", "png,jpg",
            ],
        )

    def test_a_missing_source_is_refused(self):
        rc, _out, err = self.run_shim(shortcuts.convx_main, ["--format", "webp"])

        self.assertEqual(rc, 2)
        self.assertIn("usage: watch-convx SRC", err)
        self.assertEqual(self.seen, [])

    def test_help_says_the_conversion_is_in_place(self):
        """convx converts where the file sits, so there is no destination to
        take — the shim's shape differs from the other two on purpose."""
        rc, out, _err = self.run_shim(shortcuts.convx_main, ["--help"])

        self.assertEqual(rc, 0)
        self.assertIn("in-place", out)
        self.assertEqual(self.seen, [])

    def test_a_preset_is_handed_over_untouched(self):
        rc, _out, _err = self.run_shim(shortcuts.convx_main, ["--preset", "thumbs"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["--preset", "thumbs"])


class TestModuleEntryPoint(ShimCase):
    """`python -m folder_watch.shortcuts <name> ...`, so bin/watch-* works
    straight from a checkout with nothing installed."""

    def test_the_first_argument_picks_the_shim(self):
        rc, _out, _err = self.run_shim(
            shortcuts.main, ["copy", "./Out", "./Drive"]
        )

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["copy", "./Out", "--to", "./Drive"])

    def test_no_arguments_lists_the_shims(self):
        rc, _out, err = self.run_shim(shortcuts.main, [])

        self.assertEqual(rc, 2)
        for name in ("copy", "unzip", "convx"):
            self.assertIn(name, err)

    def test_an_unknown_name_is_refused(self):
        rc, _out, err = self.run_shim(shortcuts.main, ["rsync", "./a"])

        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        self.assertEqual(self.seen, [])

    def test_an_omitted_argv_falls_back_to_the_command_line(self):
        argv = sys.argv
        sys.argv = ["watch-copy", "./Out", "./Drive"]
        try:
            rc, _out, _err = self.run_shim(shortcuts.copy_main, None)
        finally:
            sys.argv = argv

        self.assertEqual(rc, 0)
        self.assertEqual(self.built(), ["copy", "./Out", "--to", "./Drive"])


if __name__ == "__main__":
    unittest.main()
