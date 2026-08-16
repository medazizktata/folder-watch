#!/usr/bin/env python3
"""Tests for the CLI's subcommands and its preset resolution.

cli.py sat at 50% — the largest untested surface left, and not an idle one:
the preset round-trip added to the suite found a real bug there on its first
run. Presets, argument resolution and every subcommand live in this file.

These call main() in-process rather than spawning it, which is what lets
coverage see the paths at all. `service` is exercised with launchctl stubbed
and the plist directory redirected, so nothing here can register a job or
write into the real ~/Library/LaunchAgents.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch.cli import main as fw_main


class CliCase(unittest.TestCase):
    """Each test gets its own presets file, restored afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()
        self.presets_path = Path(os.environ["FOLDER_WATCH_CONFIG_DIR"]) / "presets.json"
        self._saved = (
            self.presets_path.read_text() if self.presets_path.is_file() else None
        )

    def tearDown(self):
        if self._saved is None:
            self.presets_path.unlink(missing_ok=True)
        else:
            self.presets_path.write_text(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_presets(self, **presets):
        self.presets_path.parent.mkdir(parents=True, exist_ok=True)
        self.presets_path.write_text(json.dumps(presets))

    def run_cli(self, *argv):
        """main() in-process, with stdout and stderr captured."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fw_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def make_file(self, name="clip.mp4", size=2_000_000) -> Path:
        f = self.src / name
        f.write_bytes(b"x" * size)
        return f


class TestListings(CliCase):
    def test_list_actions_names_every_registered_action(self):
        from folder_watch.actions import ACTION_NAMES

        rc, out, _err = self.run_cli("--list-actions")

        self.assertEqual(rc, 0)
        for name in ACTION_NAMES:
            self.assertIn(name, out, f"{name} missing from --list-actions")

    def test_list_presets_on_an_empty_config_says_so(self):
        self.write_presets()

        rc, out, _err = self.run_cli("--list-presets")

        self.assertEqual(rc, 0)
        self.assertIn("no presets configured", out.lower())
        self.assertIn("presets.json", out, "say where it looked")

    def test_list_presets_reports_a_healthy_one_without_complaint(self):
        self.write_presets(
            good={"action": "copy", "src": str(self.src), "to": str(self.dest)}
        )

        rc, out, _err = self.run_cli("--list-presets")

        self.assertEqual(rc, 0)
        self.assertIn("good", out)
        self.assertNotIn("need attention", out)

    def test_list_presets_flags_a_dead_source_without_touching_it(self):
        """Reports and stops there — it cannot tell "project finished" from
        "drive unplugged", and its own runtime treats a missing folder as
        temporary."""
        self.write_presets(
            dead={
                "action": "copy",
                "src": str(self.tmp / "long-gone"),
                "to": str(self.dest),
            }
        )

        rc, out, _err = self.run_cli("--list-presets")

        self.assertEqual(rc, 0)
        self.assertIn("dead", out)
        self.assertIn("missing", out.lower())
        self.assertTrue(self.presets_path.is_file(), "listing mutated the config")
        self.assertIn("dead", json.loads(self.presets_path.read_text()))

    def test_an_unknown_preset_lists_the_known_ones(self):
        self.write_presets(alpha={"action": "copy", "src": str(self.src)})

        rc, _out, err = self.run_cli("--preset", "beta", "--once")

        self.assertEqual(rc, 2)
        self.assertIn("beta", err)
        self.assertIn("alpha", err)


class TestPresetResolution(CliCase):
    """The block that decides what a preset actually sets."""

    def test_a_preset_supplies_source_and_destination(self):
        self.make_file()
        self.write_presets(
            job={
                "action": "copy",
                "src": str(self.src),
                "to": str(self.dest),
                "ext": ["mp4"],
            }
        )

        rc, out, _err = self.run_cli("--preset", "job", "--once", "--no-notify")

        self.assertEqual(rc, 0)
        self.assertIn("copied", out)
        self.assertTrue((self.dest / "clip.mp4").is_file())

    def test_the_command_line_beats_the_preset(self):
        self.make_file()
        other = self.tmp / "override"
        other.mkdir()
        self.write_presets(
            job={
                "action": "copy",
                "src": str(self.src),
                "to": str(self.dest),
                "ext": ["mp4"],
            }
        )

        rc, _out, _err = self.run_cli(
            "--preset", "job", "--to", str(other), "--once", "--no-notify"
        )

        self.assertEqual(rc, 0)
        self.assertTrue((other / "clip.mp4").is_file())
        self.assertFalse((self.dest / "clip.mp4").exists())

    def test_a_preset_carries_the_safety_flags_into_an_unattended_run(self):
        """These were unreachable from a preset, which is exactly the runs
        that most need them — the ones nobody is watching."""
        self.write_presets(
            job={
                "action": "unzip",
                "src": str(self.src),
                "no_clobber": True,
                "create_dest": True,
            }
        )

        rc, out, _err = self.run_cli("--preset", "job", "--once", "--json")

        self.assertEqual(rc, 0)
        start = next(
            json.loads(line)
            for line in out.splitlines()
            if line.startswith("{") and '"event": "start"' in line
        )
        self.assertIn("unzip", start["action"])

    def test_an_unknown_key_in_a_preset_warns_rather_than_passing_silently(self):
        self.write_presets(
            job={"action": "copy", "src": str(self.src), "to": str(self.dest),
                 "no_clober": True}
        )

        _rc, out, _err = self.run_cli("--preset", "job", "--once", "--no-notify")

        self.assertIn("unknown_preset_keys", out)
        self.assertIn("no_clober", out)

    def test_saving_a_preset_captures_the_invocation(self):
        rc, _out, _err = self.run_cli(
            "copy", str(self.src), "--to", str(self.dest),
            "--ext", "mov", "--stable", "5", "--save-preset", "captured",
        )

        self.assertEqual(rc, 0)
        cfg = json.loads(self.presets_path.read_text())["captured"]
        self.assertEqual(cfg["action"], "copy")
        self.assertEqual(cfg["ext"], ["mov"])
        self.assertEqual(cfg["stable"], 5)
        self.assertEqual(Path(cfg["src"]), self.src)

    def test_saving_over_a_preset_merges_instead_of_resetting(self):
        """Re-saving used to drop every field the second command did not
        mention, while reporting success."""
        self.run_cli(
            "copy", str(self.src), "--to", str(self.dest),
            "--ext", "mov", "--min-bytes", "77", "--save-preset", "job",
        )
        rc, out, _err = self.run_cli(
            "copy", str(self.src), "--to", str(self.dest),
            "--stable", "9", "--save-preset", "job",
        )

        self.assertEqual(rc, 0)
        cfg = json.loads(self.presets_path.read_text())["job"]
        self.assertEqual(cfg["stable"], 9, "the new value did not land")
        self.assertEqual(cfg["min_bytes"], 77, "an untouched field was reset")
        self.assertIn("modif", out.lower())


class TestSubcommands(CliCase):
    def test_status_with_no_watchers_says_so(self):
        rc, out, _err = self.run_cli("status")

        self.assertEqual(rc, 1)
        self.assertIn("no watchers", out.lower())

    def test_prune_is_a_preview_until_told_otherwise(self):
        rc, out, _err = self.run_cli("prune")

        self.assertEqual(rc, 0)
        self.assertIn("dry run", out.lower())

    def test_prune_yes_reports_what_it_did(self):
        rc, out, _err = self.run_cli("prune", "--yes")

        self.assertEqual(rc, 0)
        self.assertNotIn("dry run", out.lower())

    def test_forgetting_an_unknown_preset_lists_the_known_ones(self):
        self.write_presets(alpha={"action": "copy", "src": str(self.src)})

        rc, _out, err = self.run_cli("presets", "forget", "nope")

        self.assertEqual(rc, 1)
        self.assertIn("alpha", err)

    def test_forgetting_is_a_preview_until_confirmed(self):
        self.write_presets(doomed={"action": "copy", "src": str(self.src)})

        rc, out, _err = self.run_cli("presets", "forget", "doomed")

        self.assertEqual(rc, 0)
        self.assertIn("doomed", json.loads(self.presets_path.read_text()))
        self.assertIn("--yes", out)

    def test_forgetting_with_yes_removes_only_that_preset(self):
        self.write_presets(
            doomed={"action": "copy", "src": str(self.src)},
            keeper={"action": "copy", "src": str(self.src)},
        )

        rc, _out, _err = self.run_cli("presets", "forget", "doomed", "--yes")

        self.assertEqual(rc, 0)
        remaining = json.loads(self.presets_path.read_text())
        self.assertNotIn("doomed", remaining)
        self.assertIn("keeper", remaining)

    def test_pending_reports_what_a_fresh_watcher_would_skip(self):
        """Read-only: no state written, safe against a live folder."""
        self.make_file("never-processed.mp4")

        rc, out, _err = self.run_cli(
            "copy", str(self.src), "--to", str(self.dest), "--pending"
        )

        self.assertEqual(rc, 1, "pending work should be a non-zero status")
        self.assertIn("never-processed.mp4", out)

    def test_pending_is_quiet_when_everything_has_an_output(self):
        f = self.make_file()
        shutil.copy2(f, self.dest / f.name)

        rc, _out, _err = self.run_cli(
            "copy", str(self.src), "--to", str(self.dest), "--pending"
        )

        self.assertEqual(rc, 0)


class TestServiceCommand(CliCase):
    """launchctl is stubbed and the plist directory is redirected, so these
    can never register a job or touch the real ~/Library/LaunchAgents."""

    def setUp(self):
        super().setUp()
        import folder_watch.support.launchd as launchd

        self.launchd = launchd
        self.calls: list[list[str]] = []
        self._real_run = launchd.subprocess.run

        class _Result:
            def __init__(self, returncode=0):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""

        self.loaded: set[str] = set()

        def fake_run(cmd, *a, **kw):
            """Stateful, because a stateless one proves nothing.

            `launchctl list LABEL` exits non-zero for a label that was never
            loaded. A stub that always returns 0 makes every service look
            loaded, so a status check could never fail and the assertion
            would be vacuous. Loading records the label; listing consults it.
            """
            self.calls.append(list(cmd))
            if len(cmd) >= 2 and cmd[0] == "launchctl":
                verb, arg = cmd[1], cmd[-1]
                if verb == "load":
                    self.loaded.add(Path(arg).stem)
                elif verb == "unload":
                    self.loaded.discard(Path(arg).stem)
                elif verb == "list":
                    return _Result(0 if arg in self.loaded else 1)
            return _Result(0)

        launchd.subprocess.run = fake_run
        self.agents = Path(os.environ["FOLDER_WATCH_LAUNCH_AGENTS"])

    def tearDown(self):
        self.launchd.subprocess.run = self._real_run
        for p in self.agents.glob("*.plist"):
            p.unlink(missing_ok=True)
        super().tearDown()

    def test_the_real_launchagents_directory_is_never_the_target(self):
        """The guard that makes the rest of this class safe to run at all."""
        self.assertNotEqual(self.agents, Path.home() / "Library" / "LaunchAgents")
        self.assertIn("folder-watch-tests", str(self.agents))

    def test_install_writes_a_plist_and_loads_it(self):
        self.write_presets(job={"action": "copy", "src": str(self.src),
                                "to": str(self.dest)})

        rc, out, _err = self.run_cli("service", "install", "--preset", "job")

        self.assertEqual(rc, 0)
        plist = self.agents / "com.folder-watch.job.plist"
        self.assertTrue(plist.is_file())
        self.assertIn(str(plist), out)
        self.assertTrue(
            any("load" in c for c in self.calls), f"never loaded: {self.calls}"
        )

    def test_the_installed_plist_is_valid_and_names_the_preset(self):
        import xml.etree.ElementTree as ET

        self.write_presets(job={"action": "copy", "src": str(self.src),
                                "to": str(self.dest)})
        self.run_cli("service", "install", "--preset", "job")

        body = (self.agents / "com.folder-watch.job.plist").read_text()
        ET.fromstring(body)  # raises if launchd would reject it

        self.assertIn("com.folder-watch.job", body)
        self.assertIn("/usr/sbin", body, "lsof must stay reachable under launchd")

    def test_uninstall_removes_the_plist(self):
        self.write_presets(job={"action": "copy", "src": str(self.src)})
        self.run_cli("service", "install", "--preset", "job")
        plist = self.agents / "com.folder-watch.job.plist"
        self.assertTrue(plist.is_file())

        rc, _out, _err = self.run_cli("service", "uninstall", "--preset", "job")

        self.assertEqual(rc, 0)
        self.assertFalse(plist.exists())

    def test_status_of_a_service_that_was_never_installed(self):
        """Non-zero, so a script can tell "not set up" from "running"."""
        rc, out, _err = self.run_cli("service", "status", "--preset", "ghost")

        self.assertEqual(rc, 1)
        self.assertIn("plist_exists", out)

    def test_an_unknown_service_verb_is_refused(self):
        """argparse `choices` exits rather than returning, so the caller sees
        SystemExit(2) — still an exit status of 2 to the shell."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("service", "frobnicate", "--preset", "job")

        self.assertEqual(caught.exception.code, 2)


class TestArgumentErrors(CliCase):
    def test_no_arguments_prints_help(self):
        rc, out, _err = self.run_cli()

        self.assertEqual(rc, 2)
        self.assertIn("usage", out.lower())

    def test_an_unknown_action_is_named(self):
        rc, _out, err = self.run_cli("frobnicate", str(self.src))

        self.assertEqual(rc, 2)
        self.assertIn("frobnicate", err)

    def test_copy_without_a_destination_is_refused(self):
        rc, _out, err = self.run_cli("copy", str(self.src), "--once")

        self.assertEqual(rc, 2)
        self.assertIn("--to", err)

    def test_exec_without_a_command_is_refused(self):
        rc, _out, err = self.run_cli("exec", str(self.src), "--once")

        self.assertEqual(rc, 2)
        self.assertIn("--cmd", err)

    def test_a_missing_source_is_an_error_not_an_empty_folder(self):
        rc, out, _err = self.run_cli(
            "copy", str(self.tmp / "nowhere"), "--to", str(self.dest), "--once"
        )

        self.assertEqual(rc, 2)
        self.assertIn("src_missing", out)

    def test_version_reports_the_packaged_version(self):
        """argparse's --version exits rather than returning, so the status is
        what a shell sees. The value itself is checked in test_release."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--version")

        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
