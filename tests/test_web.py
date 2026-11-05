"""The web dashboard: a real server on a free port, real HTTP requests.

What matters most here is who may do what: every account on the Mac and any
page in the browser can reach localhost, so nothing is served without the key
from a file only its owner can read, actions must come from the page's own
origin, and requests for any other host name are refused. launchctl and
Finder are faked; tests/e2e drives the real command.
"""
from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import web
from folder_watch.presets import remove_preset, save_preset


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.preset = f"web-{id(self)}"
        cfg = {"action": "copy", "src": str(self.tmp), "to": str(self.tmp)}
        save_preset(self.preset, cfg)
        self.addCleanup(remove_preset, self.preset)
        # Fresh, so creating it is tested too, not only reading one left behind.
        key_file = mock.patch.object(web, "KEY_FILE", self.tmp / "dashboard.key")
        key_file.start()
        self.addCleanup(key_file.stop)
        self.server = web.DashboardServer(0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, host=None, key="ok", origin=None, body=None):
        """As the page would send it: with the cookie, unless key is None."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if key is not None:
            key = web.dashboard_key() if key == "ok" else key
            headers["Cookie"] = f"{web.COOKIE}={key}"
        if origin is not None:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        payload = r.read().decode()
        self.headers = r.headers
        conn.close()
        return r.status, payload

    def act(self, action, preset=None, key="ok", origin=None, extra=None):
        status, payload = self.request(
            "POST", f"/api/{action}", key=key, origin=origin,
            body={"preset": preset or self.preset, **(extra or {})},
        )
        return status, json.loads(payload)


class TestReading(WebCase):
    def test_the_page_and_the_rows_are_served_with_the_key(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>folder-watch</title>", page)
        self.assertNotIn(web.dashboard_key(), page, "the page must not hand it out")

        status, rows = self.request("GET", "/api/rows")
        self.assertEqual(status, 200)
        self.assertIn(self.preset, [r["preset"] for r in json.loads(rows)])

    def test_starting_does_no_reverse_dns_lookup(self):
        import socket

        with mock.patch.object(socket, "getfqdn",
                               side_effect=AssertionError("reverse DNS lookup")):
            server = web.DashboardServer(0)
        server.server_close()
        self.assertEqual(server.server_name, "127.0.0.1")

    def test_it_listens_on_this_machine_only(self):
        """Bound to every interface, anyone on the same Wi-Fi could load it."""
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_another_host_name_is_refused(self):
        """A domain rebound to 127.0.0.1 would otherwise read everything."""
        for path in ("/", "/api/rows"):
            status, _ = self.request("GET", path, host=f"evil.example:{self.port}")
            self.assertEqual(status, 403, path)


class TestKey(WebCase):
    """Another account on this Mac reaches 127.0.0.1 too, and used to read
    the page that carried the token."""

    def test_nothing_is_served_without_the_key(self):
        paths = ("/", "/api/rows", f"/api/log?preset={self.preset}", "/api/audit")
        for key in (None, "guess"):
            for path in paths:
                with self.subTest(key=key, path=path):
                    status, body = self.request("GET", path, key=key)
                    self.assertEqual(status, 403)
                    self.assertNotIn(web.dashboard_key(), body)
                    self.assertNotIn(self.preset, body)

    def test_the_link_trades_the_key_for_a_cookie_once(self):
        key = web.dashboard_key()
        status, _ = self.request("GET", f"/?key={key}", key=None)
        self.assertEqual(status, 303)
        self.assertEqual(self.headers["Location"], "/")
        cookie = self.headers["Set-Cookie"]
        self.assertIn(f"{web.COOKIE}={key}", cookie)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)

        status, _ = self.request("GET", "/?key=guess", key=None)
        self.assertEqual(status, 403)
        self.assertIsNone(self.headers["Set-Cookie"])

    def test_the_key_is_owner_only_and_the_same_for_every_dashboard(self):
        self.assertFalse(web.KEY_FILE.exists())
        key = web.dashboard_key()
        self.assertEqual(web.KEY_FILE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(web.dashboard_key(), key)
        self.assertIn(f"?key={key}", self.server.link)

    def test_two_dashboards_starting_at_once_agree_on_the_key(self):
        """The one that writes first wins; the other must not replace it, or
        the link the first one opened stops working."""
        first, second = "f" * 32, "s" * 32  # real keys are 32 characters

        def another_dashboard_wins(_n):
            web.KEY_FILE.write_text(first)
            return second

        with mock.patch.object(web.secrets, "token_urlsafe",
                               side_effect=another_dashboard_wins):
            self.assertEqual(web.dashboard_key(), first)
        self.assertEqual(web.KEY_FILE.read_text(), first)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()
                                if p.name.startswith(".dashboard-key")), [])

    def test_an_empty_or_short_key_file_is_replaced_not_trusted(self):
        """An empty key matched a request with no cookie: no login at all."""
        for leftover in ("", "short"):
            with self.subTest(leftover=leftover):
                web.KEY_FILE.write_text(leftover)
                self.assertEqual(self.request("GET", "/api/rows", key=None)[0], 403)
                self.assertEqual(self.request("GET", "/api/rows", key="")[0], 403)
                key = web.dashboard_key()
                self.assertGreaterEqual(len(key), web.MIN_KEY_LENGTH)
                self.assertEqual(web.KEY_FILE.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.request("GET", "/api/rows")[0], 200)

    def test_an_empty_cookie_never_matches_even_an_empty_key(self):
        """The second guard, should the key ever come back empty again."""
        with mock.patch.object(web, "dashboard_key", return_value=""):
            self.assertFalse(web.key_matches(""))

    def test_a_non_ascii_key_is_a_refusal_not_a_crash(self):
        self.assertEqual(self.request("GET", "/?key=%C3%A9", key=None)[0], 403)
        self.assertEqual(self.request("GET", "/api/rows", key="é")[0], 403)

    def test_the_page_cannot_be_framed(self):
        self.request("GET", "/")
        self.assertEqual(self.headers["X-Frame-Options"], "DENY")


class TestActions(WebCase):
    def test_an_action_needs_the_key(self):
        """Any site can POST to localhost; only the owner can read the key."""
        with mock.patch.object(web, "pause_service") as pause:
            self.assertEqual(self.act("pause", key=None)[0], 403)
            self.assertEqual(self.act("pause", key="guess")[0], 403)
        pause.assert_not_called()

    def test_another_origin_cannot_act_even_with_the_cookie(self):
        """A page on another localhost port is the same site to a browser,
        so SameSite sends it the cookie."""
        with mock.patch.object(web, "pause_service") as pause:
            for origin in ("http://127.0.0.1:1", "http://evil.example", "null"):
                self.assertEqual(self.act("pause", origin=origin)[0], 403, origin)
            pause.assert_not_called()
            own = f"http://127.0.0.1:{self.port}"
            self.assertEqual(self.act("pause", origin=own)[0], 200)

    def test_another_host_cannot_act_even_with_the_key(self):
        with mock.patch.object(web, "pause_service") as pause:
            status, _ = self.request(
                "POST", "/api/pause", host=f"evil.example:{self.port}",
                body={"preset": self.preset},
            )
        self.assertEqual(status, 403)
        pause.assert_not_called()

    def test_pause_and_resume_reach_launchd(self):
        with mock.patch.object(web, "pause_service") as pause:
            self.assertEqual(self.act("pause")[0], 200)
        pause.assert_called_once_with(preset=self.preset)
        with mock.patch.object(web, "resume_service") as resume:
            self.assertEqual(self.act("resume")[0], 200)
        resume.assert_called_once_with(preset=self.preset)

    def test_open_folder_reveals_the_source(self):
        with mock.patch.object(web, "reveal_in_finder") as reveal:
            self.assertEqual(self.act("folder")[0], 200)
        reveal.assert_called_once_with(self.tmp)

    def test_edit_changes_the_folders_and_nothing_else(self):
        from folder_watch.presets import load_raw_preset

        moved = self.tmp / "moved"
        # cmd and action would turn the page into a way to run any command.
        status, _ = self.act("edit", extra={"src": str(moved), "verify": "true",
                                            "action": "exec", "cmd": "/usr/bin/true"})

        self.assertEqual(status, 200)
        cfg = load_raw_preset(self.preset)
        self.assertEqual(cfg["src"], str(moved.resolve()))
        self.assertNotIn("verify", cfg, "a key the form does not offer was set")
        self.assertEqual(cfg["action"], "copy")
        self.assertNotIn("cmd", cfg)
        self.assertEqual(self.act("edit", extra={"src": " "})[0], 400)

    def test_unknown_preset_or_action_and_a_missing_log(self):
        self.assertEqual(self.act("pause", preset="no-such-preset")[0], 404)
        self.assertEqual(self.act("format-disk")[0], 404)
        status, data = self.act("log")
        self.assertEqual(status, 409)
        self.assertIn("no log", data["error"])


class TestExtras(WebCase):
    def test_the_log_tail_is_the_last_lines_only(self):
        from folder_watch.support.paths import STATE_DIR

        log = STATE_DIR / f"{self.preset}.log"
        log.write_text("".join(f"line {i}\n" for i in range(50)))
        self.addCleanup(log.unlink, True)

        status, payload = self.request("GET", f"/api/log?preset={self.preset}")

        lines = json.loads(payload)["lines"]
        self.assertEqual(status, 200)
        self.assertEqual(lines, [f"line {i}" for i in range(30, 50)])
        self.assertEqual(self.request("GET", "/api/log?preset=nope")[0], 404)
        self.assertEqual(
            self.request("GET", f"/api/log?preset={self.preset}",
                         host=f"evil.example:{self.port}")[0],
            403,
        )

    def test_doctor_and_cleanup_need_the_key(self):  # holds on BASE
        for action in ("doctor", "cleanup"):
            self.assertEqual(self.act(action, key=None)[0], 403, action)

    def test_doctor_returns_its_findings(self):
        with mock.patch.object(web.doctor, "check_services",
                               return_value=[("error", "service x: last exit 1")]):
            status, data = self.act("doctor")
        self.assertEqual(status, 200)
        self.assertIn(
            {"section": "services", "level": "error",
             "message": "service x: last exit 1"},
            data["findings"],
        )

    def test_cleanup_is_a_dry_run_unless_apply_is_exactly_true(self):
        """The page shows the dry run first; only the Apply button removes."""
        from folder_watch import cli

        cases = (({}, True), ({"apply": "yes"}, True), ({"apply": True}, False))
        for extra, dry in cases:
            with (
                mock.patch.object(cli, "_prune_state", return_value=0) as state,
                mock.patch.object(cli, "_prune_presets", return_value=[]),
                mock.patch.object(cli, "_prune_logs", return_value=0),
            ):
                status, data = self.act("cleanup", extra=extra)
            self.assertEqual(status, 200)
            self.assertEqual(state.call_args.kwargs, {"dry": dry}, extra)
            self.assertEqual(data["applied"], not dry)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_page_script_parses(self):  # holds on BASE
        """An escape that Python, not JavaScript, turned into a real newline
        once broke the whole script: no rows, no buttons, and every test that
        only looked for text in the page still passed."""
        import re
        import subprocess

        _status, page = self.request("GET", "/")
        script = self.tmp / "page.js"
        script.write_text(re.search(r"<script>(.*)</script>", page, re.S).group(1))
        r = subprocess.run(["node", "--check", str(script)], capture_output=True,
                           text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_page_has_the_tools(self):
        _status, page = self.request("GET", "/")
        for label in ("Run doctor", "Clean up", "showLog"):
            self.assertIn(label, page)


class TestPreview(WebCase):
    """The Test button: a real --once --dry-run run, nothing written."""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "in"
        self.dest = self.tmp / "out"
        self.src.mkdir()
        self.dest.mkdir()
        save_preset(self.preset, {"action": "copy", "src": str(self.src),
                                  "to": str(self.dest)})

    def test_it_says_what_would_happen_and_does_none_of_it(self):
        (self.src / "clip.mp4").write_bytes(b"x" * 2_000_000)

        status, data = self.act("test")

        self.assertEqual(status, 200, data)
        self.assertTrue(any("would copy" in line and "clip.mp4" in line
                            for line in data["lines"]), data["lines"])
        self.assertEqual(list(self.dest.iterdir()), [])
        from folder_watch.support import retention

        self.assertNotIn(self.preset, retention._read_json(retention.VOLUMES),
                         "a dry run wrote the volume note")

    def test_an_empty_folder_says_so(self):
        status, data = self.act("test")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["lines"]), 1)
        self.assertIn("nothing to do", data["lines"][0])

    def test_unknown_or_convx_presets_are_refused(self):
        self.assertEqual(self.act("test", preset="no-such-preset")[0], 404)
        save_preset(self.preset, {"action": "convx", "src": str(self.src),
                                  "format": "wav"})
        self.assertEqual(self.act("test")[0], 400)

    def test_the_page_has_the_button(self):
        _status, page = self.request("GET", "/")
        self.assertIn('"Test"', page)
        self.assertIn("/api/test", page)


class TestHomeRelative(unittest.TestCase):
    def test_home_becomes_tilde_only_where_it_is_a_whole_path(self):
        from folder_watch.dashboard import home_relative

        with mock.patch.object(Path, "home", return_value=Path("/Users/me")):
            for given, shown in (
                ("/Users/me/in", "~/in"),
                ("/Users/me", "~"),
                ("copy · /Users/me/in → /Users/me/out", "copy · ~/in → ~/out"),
                ("src missing: /Users/me", "src missing: ~"),
                # Another folder that only starts with the same letters:
                # as ~-archive it would name another user's home.
                ("/Users/me-archive/in", "/Users/me-archive/in"),
                ("/Users/meg/in", "/Users/meg/in"),
            ):
                with self.subTest(given=given):
                    self.assertEqual(home_relative(given), shown)


class TestHistory(WebCase):
    def setUp(self):
        super().setUp()
        from folder_watch.support import audit

        self.audit = audit
        patcher = mock.patch.object(audit, "AUDIT", self.tmp / "audit.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, ts, kind, preset):
        with self.audit.AUDIT.open("a") as f:
            f.write(json.dumps({"ts": ts, "kind": kind, "preset": preset}) + "\n")

    def test_files_per_preset_per_day(self):
        from datetime import date

        today = date(2026, 11, 5)
        for ts, kind in (("2026-11-05T09:00:00", "copied"),
                         ("2026-11-05T10:00:00", "copied"),
                         ("2026-11-05T11:00:00", "fail"),
                         ("2026-11-04T09:00:00", "extracted"),
                         ("2026-11-03T09:00:00", "give_up"),
                         ("2026-11-05T12:00:00", "preset_saved"),  # not a file
                         ("2026-10-01T09:00:00", "copied")):       # too old
            self.write(ts, kind, "a")
        self.write("2026-11-05T09:30:00", "ran", "b")

        data = web.history(days=3, today=today)

        self.assertEqual(data["days"], ["2026-11-03", "2026-11-04", "2026-11-05"])
        self.assertEqual(data["presets"]["a"], {"done": [0, 1, 2], "failed": [1, 0, 1]})
        self.assertEqual(data["presets"]["b"], {"done": [0, 0, 1], "failed": [0, 0, 0]})

    def test_the_endpoint_needs_the_key_and_serves_30_days(self):
        self.assertEqual(self.request("GET", "/api/history", key=None)[0], 403)
        status, payload = self.request("GET", "/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(payload)["days"]), web.HISTORY_DAYS)


class TestAuditFromTheDashboard(WebCase):
    def setUp(self):
        super().setUp()
        from folder_watch.support import audit

        self.audit = audit
        patcher = mock.patch.object(audit, "AUDIT", self.tmp / "audit.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_action_from_the_page_is_attributed_to_the_dashboard(self):
        """The server thread, not the person at a terminal, did this."""
        from folder_watch.support import launchd

        plist = launchd.service_plist_path(self.preset)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        self.addCleanup(plist.unlink, True)
        with mock.patch.object(launchd, "_launchctl"):
            self.assertEqual(self.act("pause")[0], 200)

        (entry,) = self.audit.read(kinds={"service_paused"})
        self.assertEqual(entry["trigger"], "dashboard")

    def test_the_audit_panel_lists_newest_first_with_details(self):
        self.audit.record("copied", preset="a", name="first.jpg")
        self.audit.record("fail", preset="a", name="second.jpg")

        status, payload = self.request("GET", "/api/audit?since=1d")

        entries = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual([e["kind"] for e in entries], ["fail", "copied"])
        self.assertIn("name=second.jpg", entries[0]["details"])
        self.assertEqual(self.request("GET", "/api/audit?since=soon")[0], 400)


class TestDashboardService(unittest.TestCase):
    def test_the_login_service_keeps_a_fixed_port_without_looping(self):
        import plistlib

        from folder_watch.support import launchd

        with (
            mock.patch.object(
                launchd, "_resolve_python_and_root",
                return_value=(Path("/x/.venv/bin/python"), Path("/x")),
            ),
            mock.patch.object(launchd.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            plist = launchd.install_dashboard_service(
                program=Path("/x/bin/fw"), port=8765
            )
        self.addCleanup(plist.unlink, True)

        job = plistlib.loads(plist.read_bytes())
        self.assertEqual(job["ProgramArguments"][-5:],
                         ["dashboard", "--no-open", "--strict-port", "--port", "8765"])
        self.assertEqual(job["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(job["ThrottleInterval"], 300)
        self.assertTrue(job["RunAtLoad"])
        import os

        self.assertEqual(job["EnvironmentVariables"]["FOLDER_WATCH_STATE_DIR"],
                         os.environ["FOLDER_WATCH_STATE_DIR"])

    def test_strict_port_refuses_a_taken_port(self):
        import contextlib
        import io
        import socket

        from folder_watch import cli

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            err = io.StringIO()
            # If it ever fell back to another port it would serve forever and
            # hang the suite; failing fast here keeps a regression a failure.
            with (
                contextlib.redirect_stderr(err),
                contextlib.redirect_stdout(io.StringIO()),
                mock.patch.object(web.DashboardServer, "serve_forever",
                                  side_effect=AssertionError("served on another port")),
            ):
                rc = cli.run_dashboard_cli(
                    ["--strict-port", "--port", str(port), "--no-open"]
                )
        self.assertEqual(rc, 1)
        self.assertIn("not available", err.getvalue())

    def test_the_key_is_printed_only_when_asked_for(self):
        """Under launchd the address line goes to a log file."""
        import contextlib
        import io

        from folder_watch import cli

        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(web.DashboardServer, "serve_forever",
                              side_effect=KeyboardInterrupt),
        ):
            cli.run_dashboard_cli(["--port", "0", "--no-open"])
        self.assertNotIn(web.dashboard_key(), out.getvalue())
        self.assertIn("--link", out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run_dashboard_cli(["--link", "--port", "9000"])
        self.assertEqual(out.getvalue().strip(),
                         f"http://127.0.0.1:9000/?key={web.dashboard_key()}")


if __name__ == "__main__":
    unittest.main()
