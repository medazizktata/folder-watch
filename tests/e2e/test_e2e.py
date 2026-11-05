"""End-to-end: real watchers on real FSEvents, and real launchd jobs.

Each of these was first checked by hand and then lost; written down here, it
runs again on every `make e2e`.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import select
import signal
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fixtures import (
    DEADLINE,
    ROOT,
    IsolatedCase,
    LaunchdCase,
    WatcherCase,
    wait_until,
)

from folder_watch.presets import load_raw_preset, remove_preset, save_preset
from folder_watch.support.launchd import (
    build_plist,
    service_label,
    service_plist_path,
    stdio_log_path,
)
from folder_watch.support.paths import LAUNCH_AGENTS, STATE_DIR


class TestWatcher(WatcherCase):
    def test_a_dropped_file_is_copied(self):
        self.start("copy", str(self.src), "--to", str(self.dest))
        dropped = self.drop("clip.mp4")

        self.wait_for("copied", name="clip.mp4")

        self.assertEqual((self.dest / "clip.mp4").read_bytes(), dropped.read_bytes())

    def test_a_dropped_archive_is_extracted_and_removed(self):
        self.start("unzip", str(self.src))
        self.drop_zip("pack.zip", {"a.txt": b"hello"})

        self.wait_for("extracted", name="pack.zip")

        self.assertEqual((self.src / "pack" / "a.txt").read_bytes(), b"hello")
        self.assertFalse((self.src / "pack.zip").exists())

    def test_the_digest_counts_what_a_real_watcher_did(self):
        name = f"e2e-digest-{os.getpid()}"
        save_preset(name, {"action": "copy", "src": str(self.src), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        # The preset's own log, which is where the digest looks.
        self.log = STATE_DIR / f"{name}.log"
        self.addCleanup(self.log.unlink, True)
        self.start("--preset", name)
        self.drop("clip.mp4")
        self.wait_for("copied", name="clip.mp4")

        out = subprocess.run(
            [sys.executable, "-B", "-m", "folder_watch", "digest"],
            cwd=ROOT, capture_output=True, text=True, env=dict(os.environ), check=True,
        ).stdout

        self.assertIn(f"{name}: copied 1", out)

    def test_follow_moves_carries_on_in_the_folders_new_place(self):
        name = f"e2e-follow-{os.getpid()}"
        save_preset(name, {"action": "copy", "src": str(self.src), "to": str(self.dest),
                           "follow_moves": True})
        self.addCleanup(remove_preset, name)
        self.start("--preset", name)
        moved = self.tmp / "renamed"
        self.src.rename(moved)

        self.wait_for("src_followed", automatic=True)
        # The watcher restarts itself, in place, on the new path. Its second
        # baseline, not its start: a file that lands before that scan is old
        # news to it, skipped as pending (as on any start).
        self.wait_for("start", src=moved)
        self.assertTrue(wait_until(lambda: len(self.events("baseline")) >= 2),
                        "the restarted watcher took no baseline")
        self.src = moved
        self.drop("after-the-move.mp4")
        self.wait_for("copied", name="after-the-move.mp4")

        self.assertEqual(load_raw_preset(name)["src"], str(moved))
        self.assertTrue((self.dest / "after-the-move.mp4").is_file())

    def test_the_audit_trail_records_a_real_copy_and_an_edit(self):
        name = f"e2e-audit-{os.getpid()}"
        save_preset(name, {"action": "copy", "src": str(self.src), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        self.start("--preset", name)
        self.drop("audited.mp4")
        self.wait_for("copied", name="audited.mp4")
        subprocess.run(
            [sys.executable, "-B", "-m", "folder_watch", "presets", "edit", name,
             "stable=4"],
            cwd=ROOT, capture_output=True, env=dict(os.environ), check=True,
        )

        out = subprocess.run(
            [sys.executable, "-B", "-m", "folder_watch", "audit", "--preset", name,
             "--json"],
            cwd=ROOT, capture_output=True, text=True, env=dict(os.environ), check=True,
        ).stdout
        entries = [json.loads(line) for line in out.splitlines()]

        copied = [e for e in entries if e["kind"] == "copied"]
        self.assertEqual([e["name"] for e in copied], ["audited.mp4"])
        self.assertEqual(copied[0]["trigger"], "cli")
        edits = [e for e in entries if e["kind"] == "preset_saved" and not e["created"]]
        self.assertEqual(edits[-1]["changes"], {"stable": [None, 4]})

    def test_a_route_preset_sends_each_kind_of_file_its_own_way(self):
        name = f"e2e-route-{os.getpid()}"
        save_preset(name, {"action": "route", "src": str(self.src), "rules": [
            {"ext": ["zip"], "action": "unzip", "mode": "named"},
            {"ext": ["mp4"], "action": "copy", "to": str(self.dest)},
        ]})
        self.addCleanup(remove_preset, name)
        self.start("--preset", name)

        self.drop_zip("pack.zip", {"a.txt": b"hello"})
        self.drop("clip.mp4")

        self.wait_for("extracted", name="pack.zip")
        self.wait_for("copied", name="clip.mp4")
        self.assertEqual((self.src / "pack" / "a.txt").read_bytes(), b"hello")
        self.assertTrue((self.dest / "clip.mp4").is_file())
        self.assertFalse((self.dest / "pack.zip").exists(), "a zip was copied too")

    def test_a_route_retries_a_copy_that_failed(self):
        """Its rules' failures used to be dropped: no retry queue at all."""
        name = f"e2e-route-retry-{os.getpid()}"
        later = self.tmp / "not-yet" / "out"
        save_preset(name, {"action": "route", "src": str(self.src), "rules": [
            {"ext": ["mp4"], "action": "copy", "to": str(later)}]})
        self.addCleanup(remove_preset, name)
        self.start("--preset", name)

        self.drop("clip.mp4")

        self.wait_for("retry_scheduled", name="clip.mp4")

    def test_an_exec_exit_75_is_queued_for_a_retry(self):
        self.start("exec", str(self.src), "--cmd", "/bin/sh -c 'exit 75'",
                   "--stable", "1")

        self.drop("clip.mp4")

        self.wait_for("retry_scheduled", name="clip.mp4")

    def test_a_renamed_source_stops_the_watcher_with_its_new_path(self):
        """Watching for a name that will not come back used to wait forever."""
        self.start("copy", str(self.src), "--to", str(self.dest))
        moved = self.tmp / "renamed"
        self.src.rename(moved)

        event = self.wait_for("error", reason="src_moved")

        self.assertEqual(event["now"], str(moved))
        self.assertEqual(self.exit_code(), 2)


class TestDashboardCommand(IsolatedCase):
    def test_the_dashboard_serves_its_page_and_rows(self):
        name = f"e2e-web-{os.getpid()}"
        save_preset(name, {"action": "copy", "src": str(self.src), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        proc = subprocess.Popen(
            [sys.executable, "-B", "-X", "faulthandler", "-m", "folder_watch",
             "dashboard", "--no-open", "--port", "0"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=dict(os.environ),
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        # The suite's own deadline: as the first test on a fresh CI runner this
        # is the first interpreter start, and 20 s was once not enough there.
        ready, _, _ = select.select([proc.stdout], [], [], DEADLINE)
        if not ready:
            # faulthandler prints every thread's stack on SIGABRT: where it hung.
            proc.send_signal(signal.SIGABRT)
            said = proc.communicate(timeout=10)[0]
            self.fail(f"the dashboard printed no address in {DEADLINE:.0f} s; "
                      f"exit {proc.returncode}, output: {said!r}")
        url = re.search(r"http://127\.0\.0\.1:\d+/", proc.stdout.readline())
        self.assertIsNotNone(url)

        base = url.group(0)
        with self.assertRaises(urllib.error.HTTPError) as locked:
            urllib.request.urlopen(base, timeout=10)
        self.assertEqual(locked.exception.code, 403)

        # As a browser: the link with the key, which leaves a cookie behind.
        from http.cookiejar import CookieJar

        from folder_watch.web import dashboard_key
        browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        page = browser.open(f"{base}?key={dashboard_key()}", timeout=10).read().decode()
        rows = json.loads(browser.open(base + "api/rows", timeout=10).read())

        self.assertIn("<title>folder-watch</title>", page)
        self.assertIn(name, [r["preset"] for r in rows])

        # As the Run doctor button sends it.
        request = urllib.request.Request(
            base + "api/doctor", data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "Origin": base.rstrip("/")},
        )
        findings = json.loads(browser.open(request, timeout=30).read())
        self.assertIn("notifications", {f["section"] for f in findings["findings"]})
        log = json.loads(browser.open(base + f"api/log?preset={name}", timeout=10).read())
        self.assertEqual(log["lines"], [])


class TestLaunchd(LaunchdCase):
    def test_a_missing_source_costs_one_start_and_returns_with_its_folder(self):
        """A bare KeepAlive restarted a watcher with no folder every ten
        seconds, for months. PathState: once, then back when it returns."""
        name = f"e2e-{os.getpid()}"
        missing = self.tmp / "later"
        save_preset(name, {"action": "copy", "src": str(missing), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        log = STATE_DIR / f"{name}.log"
        plist = plistlib.loads(build_plist(
            label=self.unique_label("pathstate"), preset=name,
            python=Path(sys.executable), root=ROOT,
            log_path=log, stdio_path=stdio_log_path(log),
            path_env="/usr/bin:/bin:/usr/sbin", src=missing,
        ).encode())
        plist["ProgramArguments"].append("--no-notify")
        label = self.load(plist)

        self.assertTrue(wait_until(lambda: self.job(label).get("last exit code") == "2"),
                        f"no src_missing exit: {self.job(label)}")
        # Longer than launchd's 10 s restart throttle, so a restart would show.
        self.assertIsNone(wait_until(lambda: self.runs(label) > 1, timeout=13),
                          "restarted with its folder still missing")

        missing.mkdir()
        self.assertTrue(wait_until(lambda: self.job(label).get("state") == "running"),
                        f"not started when the folder appeared: {self.job(label)}")

        missing.rename(self.tmp / "gone")
        self.assertTrue(
            wait_until(lambda: self.job(label).get("state") != "running"),
            "kept running after its folder moved",
        )
        runs = self.runs(label)
        self.assertIsNone(wait_until(lambda: self.runs(label) > runs, timeout=13),
                          "restarted after its folder moved away")

    def test_a_service_comes_back_when_its_volume_is_mounted_again(self):
        """PathState alone left it stopped after the volume came back."""
        image = self.tmp / "vol.dmg"
        mount_point = self.tmp / "mnt"
        mount_point.mkdir()
        subprocess.run(["hdiutil", "create", "-size", "5m", "-fs", "APFS",
                        "-volname", "fw-e2e", "-quiet", str(image)], check=True)

        def attach():
            subprocess.run(["hdiutil", "attach", "-nobrowse", "-quiet",
                            "-mountpoint", str(mount_point), str(image)], check=True)
            self.addCleanup(subprocess.run, ["hdiutil", "detach", "-force", "-quiet",
                                             str(mount_point)], capture_output=True)

        def detach():
            subprocess.run(["hdiutil", "detach", "-quiet", str(mount_point)],
                           check=True)

        attach()
        src = mount_point / "in"
        src.mkdir()
        name = f"e2e-mount-{os.getpid()}"
        save_preset(name, {"action": "copy", "src": str(src), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        detach()
        log = STATE_DIR / f"{name}.log"
        plist = plistlib.loads(build_plist(
            label=self.unique_label("mount"), preset=name,
            python=Path(sys.executable), root=ROOT,
            log_path=log, stdio_path=stdio_log_path(log),
            path_env="/usr/bin:/bin:/usr/sbin", src=src,
        ).encode())
        plist["ProgramArguments"].append("--no-notify")
        label = self.load(plist)
        self.assertTrue(wait_until(lambda: self.job(label).get("last exit code") == "2"),
                        f"no src_missing exit: {self.job(label)}")

        attach()

        self.assertTrue(wait_until(lambda: self.job(label).get("state") == "running"),
                        f"not started when its volume came back: {self.job(label)}")

    def test_a_volume_ejected_under_a_loaded_service_does_not_make_it_loop(self):
        """Loaded while mounted, then ejected, then restarted (kickstart -k,
        as for an upgrade): launchd misses a path that vanished by unmount,
        and a watcher that exited for it was restarted every 10 s."""
        image = self.tmp / "vol.dmg"
        mount_point = self.tmp / "mnt"
        mount_point.mkdir()
        subprocess.run(["hdiutil", "create", "-size", "5m", "-fs", "APFS",
                        "-volname", "fw-e2e-ej", "-quiet", str(image)], check=True)

        def attach():
            subprocess.run(["hdiutil", "attach", "-nobrowse", "-quiet",
                            "-mountpoint", str(mount_point), str(image)], check=True)

        self.addCleanup(subprocess.run, ["hdiutil", "detach", "-force", "-quiet",
                                         str(mount_point)], capture_output=True)
        attach()
        src = mount_point / "in"
        src.mkdir()
        name = f"e2e-eject-{os.getpid()}"
        save_preset(name, {"action": "copy", "src": str(src), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        log = STATE_DIR / f"{name}.log"
        # The label service install gives it: how the watcher knows launchd
        # started it (launchd sets XPC_SERVICE_NAME to it).
        plist = plistlib.loads(build_plist(
            label=service_label(name), preset=name,
            python=Path(sys.executable), root=ROOT,
            log_path=log, stdio_path=stdio_log_path(log),
            path_env="/usr/bin:/bin:/usr/sbin", src=src,
        ).encode())
        plist["ProgramArguments"].append("--no-notify")
        label = self.load(plist)
        # "running" comes as the process spawns; the baseline line means the
        # watcher is up and has noted the volume its source is on.
        started = wait_until(lambda: log.is_file() and "baseline" in log.read_text())
        self.assertTrue(started, f"never started: {self.job(label)}")

        subprocess.run(["hdiutil", "detach", "-force", "-quiet", str(mount_point)],
                       check=True)
        subprocess.run(["launchctl", "kickstart", "-k", f"{self.domain}/{label}"],
                       check=True)

        # Longer than launchd's 10 s restart throttle, so a loop would show.
        self.assertIsNone(wait_until(lambda: self.runs(label) > 3, timeout=25),
                          f"restarting in a loop: {self.job(label)}")
        attach()
        self.assertTrue(wait_until(lambda: self.job(label).get("state") == "running"))

    def test_pause_keeps_a_service_stopped_until_resume(self):
        # A fixed name: launchd keeps an enable/disable record per label for
        # good, so a per-run name would add one more on every run.
        name = "e2e-pause"
        save_preset(name, {"action": "copy", "src": str(self.src), "to": str(self.dest)})
        self.addCleanup(remove_preset, name)
        label = service_label(name)
        log = STATE_DIR / f"{name}.log"
        plist = plistlib.loads(build_plist(
            label=label, preset=name, python=Path(sys.executable), root=ROOT,
            log_path=log, stdio_path=stdio_log_path(log),
            path_env="/usr/bin:/bin:/usr/sbin", src=self.src,
        ).encode())
        plist["ProgramArguments"].append("--no-notify")
        self.load(plist)
        # resume loads from the installed location, as it would for real.
        installed = service_plist_path(name)
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes((self.tmp / f"{label}.plist").read_bytes())
        self.addCleanup(installed.unlink, True)
        # Never leave a disabled record in launchd, whatever happens below.
        self.addCleanup(subprocess.run, ["launchctl", "enable", f"{self.domain}/{label}"],
                        capture_output=True)
        self.assertTrue(wait_until(lambda: self.job(label).get("state") == "running"))

        self.cli("service", "pause", "--preset", name)
        self.assertTrue(
            wait_until(lambda: not self.job(label)), "still loaded after pause"
        )
        self.assertIn("paused", self.dashboard_row(name))

        self.cli("service", "resume", "--preset", name)
        self.assertTrue(wait_until(lambda: self.job(label).get("state") == "running"),
                        "not running after resume")
        self.assertIn("running", self.dashboard_row(name))

    def cli(self, *argv: str) -> str:
        r = subprocess.run(
            [sys.executable, "-B", "-m", "folder_watch", *argv],
            cwd=ROOT, capture_output=True, text=True, env=dict(os.environ), check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def dashboard_row(self, name: str) -> str:
        return next(
            line for line in self.cli("status", "--all").splitlines()
            if line.startswith(name)
        )

    def test_doctor_reports_a_service_that_dies_on_start(self):
        """A crash before folder_watch loads writes no log; doctor reads the
        exit code from launchd."""
        label = self.load({
            "Label": self.unique_label("crash"),
            "ProgramArguments": ["/usr/bin/false"],
            "RunAtLoad": True,
        })
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        installed = LAUNCH_AGENTS / f"{label}.plist"
        installed.write_bytes((self.tmp / f"{label}.plist").read_bytes())
        self.addCleanup(installed.unlink, True)
        self.assertTrue(wait_until(lambda: self.job(label).get("last exit code") == "1"))

        r = subprocess.run(
            [sys.executable, "-B", "-m", "folder_watch", "doctor", "--no-probe"],
            cwd=ROOT, capture_output=True, text=True, env=dict(os.environ), check=False,
        )

        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("last exit 1", r.stdout)


if __name__ == "__main__":
    unittest.main()
