#!/usr/bin/env python3
"""Guards on the release metadata.

A version that disagrees with the changelog, or a `--version` that disagrees
with what pip installed, is the exact failure a release process exists to
prevent — and it is invisible until someone is trying to work out which build
they are running.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch._version import __version__

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$", re.M)


class TestVersion(unittest.TestCase):
    def test_the_version_is_a_semantic_version(self):
        self.assertRegex(__version__, SEMVER)

    def test_the_cli_reports_the_same_version_the_package_carries(self):
        """`pyproject` reads __version__ through hatch, so these three agree by
        construction — right up until someone hardcodes one of them."""
        out = subprocess.run(
            [sys.executable, "-m", "folder_watch.cli", "--version"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        self.assertIn(__version__, out.stdout + out.stderr)

    def test_the_build_backend_reads_the_same_file(self):
        """If this moves, the wheel's metadata and `--version` drift apart."""
        pyproject = (ROOT / "pyproject.toml").read_text()

        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn("[tool.hatch.version]", pyproject)
        self.assertIn('path = "folder_watch/_version.py"', pyproject)


class TestChangelog(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "CHANGELOG.md").read_text()
        self.headings = HEADING.findall(self.text)

    def test_it_opens_with_an_unreleased_section(self):
        """Where work in progress is recorded. bin/release promotes it, and
        refuses to cut a release while it is empty."""
        self.assertEqual(self.headings[0][0], "Unreleased")

    def test_the_newest_release_matches_the_package_version(self):
        released = [name for name, _date in self.headings if name != "Unreleased"]

        self.assertTrue(released, "no released versions recorded")
        self.assertEqual(
            released[0],
            __version__,
            "the top changelog entry and __version__ have drifted apart",
        )

    def test_released_versions_are_semantic_and_in_descending_order(self):
        released = [name for name, _date in self.headings if name != "Unreleased"]
        for name in released:
            with self.subTest(version=name):
                self.assertRegex(name, SEMVER)

        keys = [tuple(int(p) for p in name.split(".")) for name in released]
        self.assertEqual(keys, sorted(keys, reverse=True), "versions out of order")

    def test_every_version_heading_has_a_link_definition(self):
        """Otherwise the compare links at the bottom render as literal
        brackets, which is how a changelog quietly rots."""
        for name, _date in self.headings:
            with self.subTest(version=name):
                self.assertIn(f"[{name}]:", self.text)


class TestReleaseScript(unittest.TestCase):
    """The guards, not the release itself — this must never tag anything."""

    def run_release(self, *args):
        return subprocess.run(
            ["./bin/release", *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

    def test_it_is_executable(self):
        self.assertTrue((ROOT / "bin" / "release").stat().st_mode & 0o111)

    def test_it_refuses_a_version_that_is_not_semantic(self):
        for bad in ("1.2", "v1.2.3", "1.2.3-rc1", "next"):
            with self.subTest(version=bad):
                result = self.run_release(bad)
                self.assertEqual(result.returncode, 2)
                self.assertIn("X.Y.Z", result.stderr)

    def test_it_refuses_to_re_cut_the_current_version(self):
        result = self.run_release(__version__)

        self.assertEqual(result.returncode, 2)
        self.assertIn("already at", result.stderr)

    def test_it_says_how_to_use_it_when_given_nothing(self):
        result = self.run_release()

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
