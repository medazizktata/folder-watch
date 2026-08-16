#!/usr/bin/env python3
"""Checksum verification, the webhook, and `presets show`.

The three remaining half-built items from the original list. Each is tested
for the property that made it worth building rather than for the feature
existing:

* verify has to catch the one corruption a size check cannot — the right
  number of wrong bytes — and must not leave the bad copy in place;
* the webhook must never be able to slow down or take down a watcher, and
  must never put the endpoint URL in a log file;
* `presets show` has to say where a value came from, since a preset defined
  in two files is merged and until now nothing said so.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)
from _loopharness import captured, fields

from folder_watch.support import webhook


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        self.src.mkdir()
        self.dest.mkdir()

    def tearDown(self):
        webhook.configure(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self, name="clip.mp4", size=200_000) -> Path:
        f = self.src / name
        f.write_bytes(os.urandom(size))
        return f


class TestChecksumVerify(Case):
    def test_a_good_copy_passes(self):
        from folder_watch.actions.copy import CopyAction

        src = self.payload()

        with captured():
            result = CopyAction(self.dest, notify_on_done=False, verify=True).run(src)

        self.assertEqual(result, "copied")
        self.assertEqual((self.dest / src.name).read_bytes(), src.read_bytes())

    def test_it_catches_the_right_number_of_wrong_bytes(self):
        """The one failure a size check is blind to. Rare on local disk, less
        rare across a network mount or a syncing client, and silent."""
        import folder_watch.actions.copy as copy_mod
        from folder_watch.actions.copy import CopyAction

        src = self.payload()
        real = copy_mod.atomic_copy

        def corrupting(s, d, **kw):
            real(s, d, **kw)
            data = bytearray(d.read_bytes())
            data[5] ^= 0xFF  # one flipped bit; length unchanged
            d.write_bytes(bytes(data))

        copy_mod.atomic_copy = corrupting
        try:
            with captured() as log:
                result = CopyAction(self.dest, notify_on_done=False, verify=True).run(src)
        finally:
            copy_mod.atomic_copy = real

        self.assertEqual(result, "retry", "corruption should be retried, not accepted")
        self.assertEqual(fields(log, "fail")[0]["reason"], "verify_failed")

    def test_a_corrupt_copy_is_removed_rather_than_left_looking_finished(self):
        import folder_watch.actions.copy as copy_mod
        from folder_watch.actions.copy import CopyAction

        src = self.payload()
        real = copy_mod.atomic_copy

        def corrupting(s, d, **kw):
            real(s, d, **kw)
            data = bytearray(d.read_bytes())
            data[0] ^= 0xFF
            d.write_bytes(bytes(data))

        copy_mod.atomic_copy = corrupting
        try:
            with captured():
                CopyAction(self.dest, notify_on_done=False, verify=True).run(src)
        finally:
            copy_mod.atomic_copy = real

        self.assertFalse(
            (self.dest / src.name).exists(),
            "a copy known to be wrong was left at the destination",
        )

    def test_without_verify_the_same_corruption_passes(self):
        """States the cost of leaving it off, and proves the flag does
        something rather than being decoration."""
        import folder_watch.actions.copy as copy_mod
        from folder_watch.actions.copy import CopyAction

        src = self.payload()
        real = copy_mod.atomic_copy

        def corrupting(s, d, **kw):
            real(s, d, **kw)
            data = bytearray(d.read_bytes())
            data[5] ^= 0xFF
            d.write_bytes(bytes(data))

        copy_mod.atomic_copy = corrupting
        try:
            with captured():
                action = CopyAction(self.dest, notify_on_done=False, verify=False)
                result = action.run(src)
        finally:
            copy_mod.atomic_copy = real

        self.assertEqual(result, "copied")

    def test_the_digest_is_streamed_not_slurped(self):
        """A 40 GB export must not be held in memory to be checked."""
        import inspect

        from folder_watch.support.copy_io import file_digest

        source = inspect.getsource(file_digest)
        self.assertIn("read(chunk)", source)
        self.assertNotIn("read_bytes()", source)

    def test_the_digest_matches_the_command_line_tool(self):
        """A checksum nobody can reproduce by hand is worth less than one they
        can, which is why this is sha256 and not something faster."""
        import hashlib
        import subprocess

        from folder_watch.support.copy_io import file_digest

        f = self.payload(size=50_000)
        out = subprocess.run(
            ["shasum", "-a", "256", str(f)], capture_output=True, text=True, check=False
        )
        expected = (
            out.stdout.split()[0]
            if out.returncode == 0
            else hashlib.sha256(f.read_bytes()).hexdigest()
        )
        self.assertEqual(file_digest(f), expected)


class TestWebhook(Case):
    def serve(self, handler_body):
        """A real HTTP server on a free port; returns (url, received)."""
        received: list[dict] = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length)))
                handler_body(self)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/hook/token", received

    @staticmethod
    def ok(handler):
        handler.send_response(200)
        handler.end_headers()

    def test_an_outcome_event_is_delivered_as_json(self):
        from folder_watch.support.log import emit

        url, received = self.serve(self.ok)
        webhook.configure(url)

        with contextlib.redirect_stdout(io.StringIO()):
            emit("copied", name="clip.mp4", size=123)
        webhook.flush(5)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["event"], "copied")
        self.assertEqual(received[0]["name"], "clip.mp4")

    def test_progress_chatter_is_not_forwarded(self):
        """progress and settle fire continuously; forwarding them would make
        the webhook a firehose that says nothing."""
        from folder_watch.support.log import emit

        url, received = self.serve(self.ok)
        webhook.configure(url)

        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(20):
                emit("progress", name="clip.mp4", size=1)
            emit("fail", name="clip.mp4", reason="dest_unavailable")
        webhook.flush(5)

        self.assertEqual([r["event"] for r in received], ["fail"])

    def test_a_dead_endpoint_does_not_slow_the_caller_down(self):
        """The property that matters most: a file copy must never wait on
        somebody else's HTTP server."""
        from folder_watch.support.log import emit

        url, _ = self.serve(lambda h: time.sleep(30))
        webhook.configure(url)

        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(5):
                emit("copied", name="clip.mp4", size=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, f"emit blocked for {elapsed:.1f}s")

    def test_an_unreachable_endpoint_never_raises(self):
        """An unreachable endpoint is a lost notification, not a lost file."""
        from folder_watch.support.log import emit

        webhook.configure("http://127.0.0.1:9/nothing-listening")

        with contextlib.redirect_stdout(io.StringIO()):
            emit("copied", name="clip.mp4", size=1)
        webhook.flush(3)

        self.assertGreaterEqual(webhook.stats()["failures"], 0)

    def test_a_flood_is_dropped_rather_than_queued_without_limit(self):
        from folder_watch.support.log import emit

        url, _ = self.serve(lambda h: time.sleep(30))
        webhook.configure(url)

        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(webhook.MAX_QUEUED + 250):
                emit("copied", name="x", size=1)

        self.assertGreater(webhook.stats()["dropped"], 0, "the queue grew unbounded")

    def test_switching_it_off_stops_delivery(self):
        from folder_watch.support.log import emit

        url, received = self.serve(self.ok)
        webhook.configure(url)
        webhook.configure(None)

        with contextlib.redirect_stdout(io.StringIO()):
            emit("copied", name="clip.mp4", size=1)
        webhook.flush(2)

        self.assertEqual(received, [])

    def test_only_the_host_is_ever_printable(self):
        """Slack and Discord put the secret in the path, and this log is
        written to disk and pasted into bug reports."""
        secret = "https://hooks.slack.com/services/T00000/B00000/VERYSECRETTOKEN"

        shown = webhook.endpoint_host(secret)

        self.assertEqual(shown, "hooks.slack.com")
        self.assertNotIn("VERYSECRETTOKEN", shown)

    def test_the_url_does_not_appear_in_the_start_line(self):
        """The obvious way to leak it: echoing the config back on startup."""
        from folder_watch.cli import main

        secret = "https://example.invalid/hook/VERYSECRETTOKEN"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main([
                "copy", str(self.src), "--to", str(self.dest),
                "--once", "--no-notify", "--no-done-store", "--webhook", secret,
            ])

        self.assertNotIn("VERYSECRETTOKEN", out.getvalue())


class TestPresetsShow(Case):
    def setUp(self):
        super().setUp()
        self.presets = Path(os.environ["FOLDER_WATCH_CONFIG_DIR"]) / "presets.json"
        self._saved = self.presets.read_text() if self.presets.is_file() else None

    def tearDown(self):
        if self._saved is None:
            self.presets.unlink(missing_ok=True)
        else:
            self.presets.write_text(self._saved)
        super().tearDown()

    def run_cli(self, *argv):
        from folder_watch.cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_it_shows_the_values_and_where_they_came_from(self):
        """A preset defined in two files is merged, and nothing used to say
        so for a single name — which is the question you have when a preset is
        not doing what you told it to."""
        self.presets.write_text(json.dumps({
            "job": {"action": "copy", "src": str(self.src), "to": str(self.dest),
                    "ext": ["mp4"]}
        }))

        rc, out, _err = self.run_cli("presets", "show", "job")

        self.assertEqual(rc, 0)
        self.assertIn("action: copy", out)
        self.assertIn(str(self.src), out)
        self.assertIn("defined in:", out)
        self.assertIn("presets.json", out)

    def test_it_reports_a_broken_preset_with_a_non_zero_status(self):
        self.presets.write_text(json.dumps({
            "dead": {
                "action": "copy",
                "src": str(self.tmp / "gone"),
                "to": str(self.dest),
            }
        }))

        rc, out, _err = self.run_cli("presets", "show", "dead")

        self.assertEqual(rc, 1, "a broken preset should be a non-zero status")
        self.assertIn("missing", out.lower())

    def test_an_unknown_name_lists_the_known_ones(self):
        self.presets.write_text(json.dumps({"alpha": {"action": "copy", "src": "/tmp"}}))

        rc, _out, err = self.run_cli("presets", "show", "nope")

        self.assertEqual(rc, 1)
        self.assertIn("alpha", err)

    def test_it_changes_nothing(self):
        before = json.dumps({"job": {"action": "copy", "src": str(self.src)}})
        self.presets.write_text(before)

        self.run_cli("presets", "show", "job")

        self.assertEqual(self.presets.read_text(), before)


if __name__ == "__main__":
    unittest.main()
