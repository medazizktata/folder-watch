"""`folder-watch dashboard`: the status --all table as a live local web page,
with buttons to pause or resume a service and to open its folder or log.

It listens on 127.0.0.1 only, which every account on the Mac and every page
in the browser can reach. So nothing is served without a key kept in a file
only you can read: the link `folder-watch dashboard` opens carries it once,
the page swaps it for an HttpOnly, SameSite=Strict cookie, and every request
after that needs the cookie. The page lives under a path derived from the key,
and the cookie is scoped to it, since a browser sends a cookie to every port
of 127.0.0.1. An action also needs to come from this page's
own origin (another port on localhost counts as the same site to a
browser), and a request addressed to any host other than this one is
refused, which stops a rebound domain from posing as localhost.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import secrets
import socketserver
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from folder_watch import dashboard, doctor
from folder_watch.presets import load_presets, parse_setting
from folder_watch.support import audit
from folder_watch.support.launchd import apply_edit, pause_service, resume_service
from folder_watch.support.notify import reveal_in_finder
from folder_watch.support.paths import STATE_DIR

REFRESH_SECONDS = 5
KEY_FILE = STATE_DIR / "dashboard.key"
COOKIE = "fw_key"
COOKIE_DAYS = 365


MIN_KEY_LENGTH = 16


def dashboard_key() -> str:
    """The secret that admits a browser, created on first use. Shared by every
    dashboard you start, including the one at login, so a link opened once
    keeps working across restarts; another account cannot read the file."""
    with contextlib.suppress(FileNotFoundError):
        key = KEY_FILE.read_text().strip()
        if len(key) >= MIN_KEY_LENGTH:
            return key
    # Missing, or empty or cut short (a crash mid-write): an empty key would
    # match a request with no cookie at all, so make a real one.
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=KEY_FILE.parent, prefix=".dashboard-key-")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp: 0600
            f.write(secrets.token_urlsafe(24))
            f.flush()
            os.fsync(f.fileno())
        try:
            # Two dashboards starting at once: the first key written is the key.
            os.link(tmp, KEY_FILE)
        except FileExistsError:
            if len(KEY_FILE.read_text().strip()) < MIN_KEY_LENGTH:
                os.replace(tmp, KEY_FILE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return KEY_FILE.read_text().strip()


def base_path() -> str:
    """Where the page and its API live, e.g. /3f9c…: the cookie is scoped to
    it, so a program on another port of 127.0.0.1 never receives it (cookies
    ignore ports). A hash of the key, not the key, as it shows in history."""
    digest = hashlib.sha256(b"folder-watch dashboard path " + dashboard_key().encode())
    return "/" + digest.hexdigest()[:16]


def key_matches(given: str) -> bool:
    """Never true for an empty `given`; bytes, so a non-ASCII guess is a
    refusal rather than a TypeError that drops the connection."""
    return bool(given) and secrets.compare_digest(
        given.encode(), dashboard_key().encode()
    )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)

    def server_bind(self) -> None:
        # HTTPServer's own version also asks for the host's full name, a
        # reverse DNS lookup that nothing here uses and that can stall on a
        # machine with slow DNS (suspected on CI, where the dashboard printed
        # nothing for 30 s).
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    @property
    def link(self) -> str:
        """The address with the key, to open once. Not for a shared log."""
        return f"{self.url}?key={dashboard_key()}"


# What the dashboard's Edit form may change. Anything else stays a job for
# `presets edit`, where every key is available.
EDITABLE = ("src", "to")


def perform(action: str, preset: str, body: dict[str, Any] | None = None) -> str:
    """Run one dashboard action; returns a line to show. Raises LookupError
    for an unknown preset or action, ValueError for a bad value, and
    RuntimeError when the action fails."""
    cfg = load_presets().get(preset)
    if cfg is None:
        raise LookupError(f"no preset {preset!r}")
    if action == "edit":
        settings = {
            key: parse_setting(key, str(body[key]))
            for key in EDITABLE
            if body and str(body.get(key) or "").strip()
        }
        if not settings:
            raise ValueError("nothing to change")
        _before, _after, reinstalled = apply_edit(preset, settings)
        suffix = " and reinstalled its service" if reinstalled else ""
        return f"updated {preset}{suffix}"
    if action == "pause":
        pause_service(preset=preset)
        return f"paused {preset}"
    if action == "resume":
        resume_service(preset=preset)
        return f"resumed {preset}"
    if action == "folder":
        reveal_in_finder(Path(str(cfg.get("src", ""))).expanduser())
        return f"opened the folder of {preset}"
    if action == "log":
        log = STATE_DIR / f"{preset}.log"
        if not log.is_file():
            raise RuntimeError(f"{preset} has no log yet")
        subprocess.run(["open", "-t", str(log)], check=False, timeout=10)
        return f"opened the log of {preset}"
    raise LookupError(f"no action {action!r}")


LOG_LINES = 20
AUDIT_ROWS = 100


def log_tail(preset: str) -> list[str]:
    if preset not in load_presets():
        raise LookupError(f"no preset {preset!r}")
    log = STATE_DIR / f"{preset}.log"
    try:
        return log.read_text("utf-8", errors="replace").splitlines()[-LOG_LINES:]
    except OSError:
        return []


PREVIEW_TIMEOUT = 60.0
HISTORY_DAYS = 30
DONE_KINDS = frozenset({"copied", "extracted", "ran"})
FAILED_KINDS = frozenset({"fail", "give_up"})


def history(days: int = HISTORY_DAYS, today: date | None = None) -> dict[str, Any]:
    """Files done and failed per preset per day, from the audit trail, oldest
    day first. Days with nothing are zeros, so every row lines up."""
    first = (today or date.today()) - timedelta(days=days - 1)
    dates = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    column = {day: i for i, day in enumerate(dates)}
    presets: dict[str, dict[str, list[int]]] = {}
    since = datetime.combine(first, datetime.min.time())
    for entry in audit.read(since=since, kinds=set(DONE_KINDS | FAILED_KINDS)):
        i = column.get(str(entry.get("ts", ""))[:10])
        name = entry.get("preset")
        if i is None or not name:
            continue
        row = presets.setdefault(str(name), {"done": [0] * days, "failed": [0] * days})
        row["done" if entry["kind"] in DONE_KINDS else "failed"][i] += 1
    return {"days": dates, "presets": presets}



def preview(preset: str) -> list[str]:
    """What the preset would do to its folder right now: a --once --dry-run
    run in a child process, which copies, extracts, runs and records nothing.
    One line per file, as the watcher reported it."""
    cfg = load_presets().get(preset)
    if cfg is None:
        raise LookupError(f"no preset {preset!r}")
    if cfg.get("action") == "convx":
        # convx watch does the work itself; there is nothing to dry-run here.
        raise ValueError("a convx preset has no dry run; try it with convx itself")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "folder_watch", "--preset", preset, "--once",
             "--dry-run", "--no-notify", "--json"],
            capture_output=True, text=True, timeout=PREVIEW_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"the dry run took over {PREVIEW_TIMEOUT:.0f} s") from e
    lines = []
    for raw in r.stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = str(event.get("event", ""))
        if kind.startswith("would_") or kind in ("error", "fail"):
            details = " ".join(
                f"{k}={v}" for k, v in event.items()
                if k not in ("event", "ts", "level", "time")
            )
            lines.append(dashboard.home_relative(f"{kind.replace('_', ' ')}: {details}"))
    return lines or ["nothing to do: no file in the folder is ready for it"]


def run_doctor() -> list[dict[str, str]]:
    """The doctor's checks without the launchd probe, which takes seconds
    per protected folder and belongs to `folder-watch doctor`."""
    sections = (
        ("notifications", doctor.check_notifiers),
        ("services", doctor.check_services),
        ("destinations", doctor.check_destinations),
    )
    return [
        {"section": title, "level": level, "message": dashboard.home_relative(message)}
        for title, check in sections
        for level, message in check()
    ]


def run_cleanup(*, apply: bool) -> str:
    """The weekly job's prune: a dry run unless `apply`, so the page can show
    what would go and remove nothing without a second click."""
    # Imported here: cli imports this module to serve the dashboard.
    from folder_watch import cli

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli._prune_state(dry=not apply)
        cli._prune_presets(dry=not apply)
        cli._prune_logs(dry=not apply)
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        pass  # a request per refresh would bury anything worth reading

    def _send(
        self, code: int, body: bytes, kind: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Framed by another localhost page, the buttons could be clicked for you.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: Any) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _origins(self) -> tuple[str, str]:
        port = self.server.server_address[1]
        return f"127.0.0.1:{port}", f"localhost:{port}"

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self._origins()

    def _key_ok(self) -> bool:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        sent = jar[COOKIE].value if COOKIE in jar else ""
        return key_matches(sent)

    def _origin_ok(self) -> bool:
        # Browsers always send Origin on a POST; a script that has the key
        # sends none and needs no protection from itself.
        origin = self.headers.get("Origin")
        return origin is None or origin in [f"http://{o}" for o in self._origins()]

    def _enter(self, given: str) -> None:
        """The plain address: the link's key, or a cookie from before the
        dashboard moved under base_path(), buys a cookie for that path only."""
        if not (key_matches(given) if given else self._key_ok()):
            return self._send(403, LOCKED.encode(), "text/html; charset=utf-8")
        prefix = base_path()
        cookie = (f"{COOKIE}={dashboard_key()}; Path={prefix}; HttpOnly; "
                  f"SameSite=Strict; Max-Age={COOKIE_DAYS * 86400}")
        self.send_response(303)
        # Off the address bar and out of the history, now that it is a cookie.
        self.send_header("Location", prefix + "/")
        self.send_header("Set-Cookie", cookie)
        # The old cookie, sent to every port on 127.0.0.1, is dropped.
        self.send_header("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "wrong host"})
        url = urlsplit(self.path)
        if url.path == "/":
            return self._enter(parse_qs(url.query).get("key", [""])[0])
        prefix = base_path()
        if not url.path.startswith(prefix + "/"):
            return self._json(404, {"error": "not found"})
        path = url.path.removeprefix(prefix)
        if not self._key_ok():
            if path == "/":
                return self._send(403, LOCKED.encode(), "text/html; charset=utf-8")
            return self._json(403, {"error": "no key: run folder-watch dashboard --link"})
        if path == "/":
            return self._send(200, page().encode(), "text/html; charset=utf-8")
        if path == "/api/rows":
            return self._json(200, dashboard.entries())
        if path == "/api/history":
            return self._json(200, history())
        if path == "/api/audit":
            try:
                since = audit.parse_since(parse_qs(url.query).get("since", ["7d"])[0])
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            entries = audit.read(since=since)[-AUDIT_ROWS:]
            return self._json(200, [
                {**e, "details": dashboard.home_relative(audit.describe(e))}
                for e in reversed(entries)
            ])
        if path == "/api/log":
            preset = parse_qs(url.query).get("preset", [""])[0]
            try:
                return self._json(200, {"lines": log_tail(preset)})
            except LookupError as e:
                return self._json(404, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "wrong host"})
        if not self._key_ok():
            return self._json(403, {"error": "no key: run folder-watch dashboard --link"})
        if not self._origin_ok():
            return self._json(403, {"error": "not from this dashboard's page"})
        prefix = base_path() + "/api/"
        if not self.path.startswith(prefix):
            return self._json(404, {"error": "not found"})
        action = self.path.removeprefix(prefix)
        # One request per thread, so this attributes just this request.
        audit.set_trigger("dashboard")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if action == "doctor":
                return self._json(200, {"findings": run_doctor()})
            if action == "test":
                return self._json(200, {"lines": preview(str(body.get("preset", "")))})
            if action == "cleanup":
                apply = body.get("apply") is True
                report = run_cleanup(apply=apply)
                return self._json(200, {"report": report, "applied": apply})
            message = perform(action, str(body.get("preset", "")), body)
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except RuntimeError as e:
            return self._json(409, {"error": str(e)})
        self._json(200, {"ok": message})


def page() -> str:
    return PAGE.replace("{{REFRESH}}", str(REFRESH_SECONDS * 1000))


LOCKED = """<!doctype html>
<meta charset="utf-8"><title>folder-watch</title>
<p>This dashboard needs its key. In Terminal, run
<code>folder-watch dashboard --link</code> and open the link it prints, once;
then bookmark the address it takes you to.</p>
"""


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>folder-watch</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6b66;
  --line: #e4e4df; --ok: #2f7d4f; --warn: #a3650c; --bad: #b3261e; --idle: #6b6b66;
  --chart-done: #8a8a84;
  --button: #efefea; --button-hover: #e4e4de;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --panel: #1f1f1d; --text: #ecece8; --muted: #9a9a93;
    --line: #2f2f2c; --ok: #6cc28f; --warn: #e0a74a; --bad: #f08a80; --idle: #9a9a93;
    --chart-done: #6b6b66;
    --button: #2a2a27; --button-hover: #34342f;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 32px 16px; }
header { display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--muted); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  margin-top: 20px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 13px; white-space: nowrap; }
.nowrap, button { white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
.name { font-weight: 600; }
.path { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 6px; vertical-align: 1px; }
.ok { background: var(--ok); } .warn { background: var(--warn); }
.error { background: var(--bad); } .idle { background: var(--idle); }
.note { color: var(--muted); font-size: 12px; }
.history-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 16px;
  align-items: end; padding: 8px 0; border-top: 1px solid var(--line); }
.history-row svg { grid-column: 1 / -1; width: 100%; max-width: 420px; height: 48px; }
.history-row .bar-done { fill: var(--chart-done); }
.history-row .bar-failed { fill: var(--bad); }
.legend { display: flex; gap: 16px; font-size: 12px; color: var(--muted);
  margin-bottom: 4px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 6px; vertical-align: -1px; }
button { font: inherit; font-size: 13px; color: var(--text); background: var(--button);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; cursor: pointer;
  margin: 0 6px 6px 0; }
button:hover { background: var(--button-hover); }
button:focus-visible { outline: 2px solid var(--ok); outline-offset: 1px; }
#toast { min-height: 20px; margin-top: 12px; color: var(--muted); }
.field { display: inline-flex; flex-direction: column; gap: 4px; margin: 0 12px 8px 0;
  color: var(--muted); font-size: 12px; }
.tools { display: flex; gap: 0; flex-wrap: wrap; }
.tool-panel { padding: 14px; }
.panel-title { font-weight: 600; margin-bottom: 8px; }
.findings { list-style: none; margin: 0 0 10px; padding: 0; }
.findings li { padding: 3px 0; }
pre { font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre-wrap; overflow-wrap: anywhere; background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: 10px;
  margin: 0 0 10px; max-height: 320px; overflow: auto; }
.field input { font: inherit; font-size: 13px; color: var(--text); background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; width: 360px;
  max-width: 100%; }
#toast.bad { color: var(--bad); }
.empty { padding: 24px 14px; color: var(--muted); }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>folder-watch</h1>
      <div class="sub">Every preset, refreshed every few seconds.
        <span id="updated"></span></div>
    </div>
    <div class="tools">
      <button id="run-doctor">Run doctor</button>
      <button id="clean-up">Clean up…</button>
      <button id="show-history">History</button>
      <button id="show-audit">Audit</button>
    </div>
  </header>
  <div class="panel">
    <table>
      <thead><tr><th>Preset</th><th>Health</th><th>Service</th><th>Last exit</th>
        <th>Last result</th><th>Log</th><th></th></tr></thead>
      <tbody id="rows"><tr><td class="empty" colspan="7">Loading…</td></tr></tbody>
    </table>
  </div>
  <div id="toast" role="status" aria-live="polite"></div>
  <div id="tool-panel" class="panel tool-panel" hidden></div>
</main>
<script>
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const size = (n) => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB"
  : (n / 1048576).toFixed(1) + " MB";
const serviceDot = (s) => s === "running" ? "ok" : s === "paused" ? "idle"
  : s === "-" ? "idle" : "error";

async function act(action, preset, extra) {
  const toast = document.getElementById("toast");
  try {
    const r = await fetch("api/" + action, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({preset, ...(extra || {})}),
    });
    const data = await r.json();
    toast.className = r.ok ? "" : "bad";
    toast.textContent = data.ok || data.error;
  } catch (e) {
    toast.className = "bad";
    toast.textContent = "The dashboard server stopped. Run folder-watch dashboard again.";
  }
  refresh();
}

function row(e) {
  const tr = el("tr");
  const name = el("td");
  name.append(el("div", "name", e.preset), el("div", "path", e.action + " · " + e.src
    + (e.to ? " → " + e.to : "")));
  const health = el("td");
  health.append(el("span", "dot " + e.level),
    el("span", "", e.level === "ok" ? "ok" : e.level));
  if (e.note) health.append(el("div", "note", e.note));
  const service = el("td", "nowrap");
  service.append(el("span", "dot " + serviceDot(e.service)),
    el("span", "", e.service === "-" ? "no service" : e.service));
  const actions = el("td");
  const button = (label, action) => {
    const b = el("button", "", label);
    b.addEventListener("click", () => act(action, e.preset));
    actions.append(b);
  };
  if (e.service === "paused") button("Resume", "resume");
  else if (e.service !== "-") button("Pause", "pause");
  button("Open folder", "folder");
  if (e.log_bytes) {
    button("Open log", "log");
    const show = el("button", "", "Log");
    show.addEventListener("click", () => showLog(e, tr));
    actions.append(show);
  }
  const edit = el("button", "", "Edit");
  edit.addEventListener("click", () => openEditor(e, tr));
  const test = el("button", "", "Test");
  test.addEventListener("click", () => testPreset(e.preset));
  actions.append(edit, test);
  tr.append(name, health, service, el("td", "", e.exit), el("td", "", e.last),
    el("td", "", e.log_bytes ? size(e.log_bytes) : "-"), actions);
  return tr;
}

let editing = null;  // a refresh would wipe a half-typed form or an open log

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  return [r.ok, await r.json()];
}

function showPanel(title, ...nodes) {
  const panel = document.getElementById("tool-panel");
  const close = el("button", "", "Close");
  close.addEventListener("click", () => { panel.hidden = true; });
  panel.replaceChildren(el("div", "panel-title", title), ...nodes, close);
  panel.hidden = false;
}

async function runDoctor() {
  showPanel("Doctor", el("div", "note", "Running…"));
  const [ok, data] = await post("api/doctor");
  if (!ok) return showPanel("Doctor", el("div", "note", data.error));
  const list = el("ul", "findings");
  for (const f of data.findings) {
    const item = el("li");
    item.append(el("span", "dot " + (f.level === "ok" ? "ok" : f.level)),
      el("span", "", f.section + ": " + f.message));
    list.append(item);
  }
  showPanel("Doctor", list);
}

async function testPreset(preset) {
  const title = "Test " + preset + " — dry run, nothing is changed";
  showPanel(title, el("div", "note", "Running…"));
  const [ok, data] = await post("api/test", {preset});
  showPanel(title, el("pre", "", ok ? data.lines.join("\\n") : data.error));
}

async function cleanUp() {
  const [ok, data] = await post("api/cleanup", {apply: false});
  if (!ok) return showPanel("Clean up", el("div", "note", data.error));
  const apply = el("button", "", "Apply");
  apply.addEventListener("click", async () => {
    const [done, result] = await post("api/cleanup", {apply: true});
    showPanel("Cleaned up", el("pre", "", done ? result.report : result.error));
    refresh();
  });
  showPanel("Clean up — dry run, nothing removed yet", el("pre", "", data.report), apply);
}

async function showLog(e, tr) {
  if (editing) return;
  const r = await fetch("api/log?preset=" + encodeURIComponent(e.preset));
  const data = await r.json();
  const row = el("tr", "editor");
  const td = el("td");
  td.colSpan = 7;
  td.append(el("pre", "log", (data.lines || []).join("\\n") || "(empty)"));
  const close = el("button", "", "Close");
  close.addEventListener("click", () => { row.remove(); editing = null; refresh(); });
  td.append(close);
  row.append(td);
  tr.after(row);
  editing = row;
}

const SVG = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// One row per preset, the same scale for all: bars are comparable down the
// panel. Failures sit on top of the done count, after a 2px gap.
function historyChart(days, row, max) {
  const w = 14, gap = 4, h = 44;
  const svg = svgEl("svg", {viewBox: `0 0 ${days.length * w} ${h}`, role: "img",
                            preserveAspectRatio: "none"});
  days.forEach((day, i) => {
    const done = row.done[i], failed = row.failed[i];
    if (!done && !failed) return;
    const g = svgEl("g", {});
    const title = svgEl("title", {});
    title.textContent = `${day}: ${done} done, ${failed} failed`;
    g.append(title);
    const x = i * w + gap / 2, bw = w - gap;
    const doneH = done ? Math.max(2, done / max * (h - 4)) : 0;
    const failH = failed ? Math.max(2, failed / max * (h - 4)) : 0;
    if (done) g.append(svgEl("rect", {class: "bar-done", x, y: h - doneH, width: bw,
                                      height: doneH, rx: failed ? 0 : 2}));
    if (failed) g.append(svgEl("rect", {class: "bar-failed", x,
                                        y: h - doneH - (done ? 2 : 0) - failH,
                                        width: bw, height: failH, rx: 2}));
    svg.append(g);
  });
  return svg;
}

async function showHistory() {
  const r = await fetch("api/history");
  const data = await r.json();
  if (!r.ok) return showPanel("History", el("div", "note", data.error));
  const names = Object.keys(data.presets).sort();
  const title = "History — files per day, last " + data.days.length + " days";
  if (!names.length) return showPanel(title, el("div", "note", "Nothing recorded yet."));
  const sum = (xs) => xs.reduce((a, b) => a + b, 0);
  const max = Math.max(1, ...names.flatMap((n) =>
    data.presets[n].done.map((d, i) => d + data.presets[n].failed[i])));
  const legend = el("div", "legend");
  for (const [cls, label] of [["bar-done", "done"], ["bar-failed", "failed"]]) {
    const item = el("span");
    const sw = el("span", "swatch");
    sw.style.background = cls === "bar-done" ? "var(--chart-done)" : "var(--bad)";
    item.append(sw, label);
    legend.append(item);
  }
  const rows = names.map((n) => {
    const p = data.presets[n];
    const box = el("div", "history-row");
    box.append(el("strong", "", n),
      el("span", "note", `${sum(p.done)} done, ${sum(p.failed)} failed`),
      historyChart(data.days, p, max));
    return box;
  });
  const span = el("div", "note", data.days[0] + " to " + data.days[data.days.length - 1]
    + ", one bar per day; hover a bar for its counts");
  showPanel(title, legend, span, ...rows);
}

async function showAudit() {
  const r = await fetch("api/audit?since=7d", {cache: "no-store"});
  const entries = await r.json();
  if (!r.ok) return showPanel("Audit", el("div", "note", entries.error));
  if (!entries.length) {
    return showPanel("Audit — last 7 days", el("div", "note", "Nothing yet."));
  }
  const table = el("table", "audit");
  for (const e of entries) {
    const tr = el("tr");
    tr.append(el("td", "nowrap", e.ts.replace("T", " ")), el("td", "nowrap", e.trigger),
      el("td", "nowrap", e.kind), el("td", "nowrap", e.preset || "-"),
      el("td", "path", e.details));
    table.append(tr);
  }
  showPanel("Audit — last 7 days, newest first", table);
}

document.getElementById("show-audit").addEventListener("click", showAudit);
document.getElementById("show-history").addEventListener("click", showHistory);
if (location.hash === "#history") showHistory();  // bookmarkable
document.getElementById("run-doctor").addEventListener("click", runDoctor);
document.getElementById("clean-up").addEventListener("click", cleanUp);

function openEditor(e, tr) {
  if (editing) return;
  const form = el("tr", "editor");
  const td = el("td");
  td.colSpan = 7;
  const field = (label, value) => {
    const wrap = el("label", "field");
    const input = el("input");
    input.value = value;
    input.spellcheck = false;
    wrap.append(el("span", "", label), input);
    td.append(wrap);
    return input;
  };
  const src = field("Watched folder", e.src);
  const to = e.action === "copy" ? field("Copy to", e.to) : null;
  const save = el("button", "", "Save");
  const cancel = el("button", "", "Cancel");
  const close = () => { form.remove(); editing = null; refresh(); };
  save.addEventListener("click", () => {
    // Only what was changed: re-sending a path re-resolves it (a symlink
    // became its target) even when you did not touch it.
    const changed = {};
    if (src.value !== e.src) changed.src = src.value;
    if (to && to.value !== e.to) changed.to = to.value;
    act("edit", e.preset, changed).then(close);
  });
  cancel.addEventListener("click", close);
  td.append(save, cancel);
  form.append(td);
  tr.after(form);
  editing = form;
  src.focus();
}

async function refresh() {
  if (editing) return;
  try {
    const r = await fetch("api/rows", {cache: "no-store"});
    const entries = await r.json();
    const body = document.getElementById("rows");
    body.replaceChildren(...(entries.length ? entries.map(row)
      : [(() => { const tr = el("tr"); const td = el("td", "empty", "No presets yet.");
          td.colSpan = 7; tr.append(td); return tr; })()]));
    document.getElementById("updated").textContent =
      "Updated " + new Date().toLocaleTimeString() + ".";
  } catch (e) {
    document.getElementById("updated").textContent = "Server not reachable.";
  }
}
refresh();
setInterval(refresh, {{REFRESH}});
</script>
</body>
</html>
"""
