"""The web dashboard: a real server on a free port, real HTTP requests.

What matters most here is who may do what: any page in the browser can send
requests to localhost, so actions need the page's token, and requests for any
other host name are refused. launchctl and Finder are faked; tests/e2e drives
the real command.
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
        self.server = web.DashboardServer(0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, host=None, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["X-FW-Token"] = token
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        payload = r.read().decode()
        conn.close()
        return r.status, payload

    def act(self, action, preset=None, token="ok", extra=None):
        token = self.server.token if token == "ok" else token
        status, payload = self.request(
            "POST", f"/api/{action}", token=token,
            body={"preset": preset or self.preset, **(extra or {})},
        )
        return status, json.loads(payload)


class TestReading(WebCase):
    def test_the_page_carries_the_token_and_the_rows_are_json(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token, page)

        status, rows = self.request("GET", "/api/rows")
        self.assertEqual(status, 200)
        self.assertIn(self.preset, [r["preset"] for r in json.loads(rows)])

    def test_it_listens_on_this_machine_only(self):
        """Bound to every interface, anyone on the same Wi-Fi could load it."""
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_another_host_name_is_refused(self):
        """A domain rebound to 127.0.0.1 would otherwise read everything."""
        for path in ("/", "/api/rows"):
            status, _ = self.request("GET", path, host=f"evil.example:{self.port}")
            self.assertEqual(status, 403, path)


class TestActions(WebCase):
    def test_an_action_needs_the_token(self):
        """Any site can POST to localhost; only this page knows the token."""
        with mock.patch.object(web, "pause_service") as pause:
            self.assertEqual(self.act("pause", token=None)[0], 403)
            self.assertEqual(self.act("pause", token="guess")[0], 403)
        pause.assert_not_called()

    def test_another_host_cannot_act_even_with_the_token(self):
        with mock.patch.object(web, "pause_service") as pause:
            status, _ = self.request(
                "POST", "/api/pause", host=f"evil.example:{self.port}",
                token=self.server.token, body={"preset": self.preset},
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
        status, _ = self.act("edit", extra={"src": str(moved), "verify": "true"})

        self.assertEqual(status, 200)
        cfg = load_raw_preset(self.preset)
        self.assertEqual(cfg["src"], str(moved.resolve()))
        self.assertNotIn("verify", cfg, "a key the form does not offer was set")
        self.assertEqual(self.act("edit", extra={"src": " "})[0], 400)

    def test_unknown_preset_or_action_and_a_missing_log(self):
        self.assertEqual(self.act("pause", preset="no-such-preset")[0], 404)
        self.assertEqual(self.act("format-disk")[0], 404)
        status, data = self.act("log")
        self.assertEqual(status, 409)
        self.assertIn("no log", data["error"])


if __name__ == "__main__":
    unittest.main()
