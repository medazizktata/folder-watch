"""`folder-watch dashboard`: the status --all table as a live local web page,
with buttons to pause or resume a service and to open its folder or log.

It listens on 127.0.0.1 only. A page from any other site can still send
requests to localhost, so every action needs a random token that only this
dashboard's own page carries (another site cannot read the page to learn it),
and a request addressed to any host other than this one is refused, which
stops a rebound domain from posing as localhost.
"""
from __future__ import annotations

import html
import json
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from folder_watch import dashboard
from folder_watch.presets import load_presets
from folder_watch.support.launchd import pause_service, resume_service
from folder_watch.support.notify import reveal_in_finder
from folder_watch.support.paths import STATE_DIR

REFRESH_SECONDS = 5


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(24)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"


def perform(action: str, preset: str) -> str:
    """Run one dashboard action; returns a line to show. Raises LookupError
    for an unknown preset or action, RuntimeError when the action fails."""
    cfg = load_presets().get(preset)
    if cfg is None:
        raise LookupError(f"no preset {preset!r}")
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


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        pass  # a request per refresh would bury anything worth reading

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: Any) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "wrong host"})
        if self.path == "/":
            body = page(self.server.token).encode()
            return self._send(200, body, "text/html; charset=utf-8")
        if self.path == "/api/rows":
            return self._json(200, dashboard.entries())
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "wrong host"})
        token = self.headers.get("X-FW-Token", "")
        if not secrets.compare_digest(token, self.server.token):
            return self._json(403, {"error": "missing or wrong token"})
        action = self.path.removeprefix("/api/")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
            message = perform(action, str(body.get("preset", "")))
        except (ValueError, LookupError) as e:
            return self._json(404, {"error": str(e)})
        except RuntimeError as e:
            return self._json(409, {"error": str(e)})
        self._json(200, {"ok": message})


def page(token: str) -> str:
    return PAGE.replace("{{TOKEN}}", html.escape(token)).replace(
        "{{REFRESH}}", str(REFRESH_SECONDS * 1000)
    )


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
  --button: #efefea; --button-hover: #e4e4de;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --panel: #1f1f1d; --text: #ecece8; --muted: #9a9a93;
    --line: #2f2f2c; --ok: #6cc28f; --warn: #e0a74a; --bad: #f08a80; --idle: #9a9a93;
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
th { color: var(--muted); font-weight: 500; font-size: 13px; }
tr:last-child td { border-bottom: 0; }
.name { font-weight: 600; }
.path { color: var(--muted); font-size: 12px; word-break: break-all; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 6px; vertical-align: 1px; }
.ok { background: var(--ok); } .warn { background: var(--warn); }
.error { background: var(--bad); } .idle { background: var(--idle); }
.note { color: var(--muted); font-size: 12px; }
button { font: inherit; font-size: 13px; color: var(--text); background: var(--button);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; cursor: pointer;
  margin: 0 6px 6px 0; }
button:hover { background: var(--button-hover); }
button:focus-visible { outline: 2px solid var(--ok); outline-offset: 1px; }
#toast { min-height: 20px; margin-top: 12px; color: var(--muted); }
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
  </header>
  <div class="panel">
    <table>
      <thead><tr><th>Preset</th><th>Health</th><th>Service</th><th>Last exit</th>
        <th>Last result</th><th>Log</th><th></th></tr></thead>
      <tbody id="rows"><tr><td class="empty" colspan="7">Loading…</td></tr></tbody>
    </table>
  </div>
  <div id="toast" role="status" aria-live="polite"></div>
</main>
<script>
const TOKEN = "{{TOKEN}}";
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

async function act(action, preset) {
  const toast = document.getElementById("toast");
  try {
    const r = await fetch("/api/" + action, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-FW-Token": TOKEN},
      body: JSON.stringify({preset}),
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
  const service = el("td");
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
  if (e.log_bytes) button("Open log", "log");
  tr.append(name, health, service, el("td", "", e.exit), el("td", "", e.last),
    el("td", "", e.log_bytes ? size(e.log_bytes) : "-"), actions);
  return tr;
}

async function refresh() {
  try {
    const r = await fetch("/api/rows", {cache: "no-store"});
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
