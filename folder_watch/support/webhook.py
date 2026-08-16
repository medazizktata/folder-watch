"""Post interesting events to an HTTP endpoint.

Hooked onto `emit` rather than onto each action, because every action already
reports through it in a structured form. An action added later is covered
without being asked to remember, which is the same reason the test harness
discovers its emit sites instead of listing them.

Three properties matter more than features here:

* **It never blocks the watch loop.** A slow or dead endpoint must not stop
  files being copied. Posts go to a bounded queue drained by one daemon
  thread; when the queue is full, events are dropped and counted rather than
  applying back-pressure to the thing doing the actual work.
* **It never crashes the watcher.** Any network error is swallowed and
  summarised; an unreachable endpoint is a lost notification, not a lost file.
* **The URL never reaches the log.** Endpoint URLs routinely carry a token in
  the path — Slack and Discord both do — and this log is written to disk and
  pasted into bug reports. Only the host is ever printed.
"""
from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

# Outcomes, not progress. `progress` and `settle` fire continuously and would
# turn a webhook into a firehose that says nothing.
DEFAULT_EVENTS = (
    "copied",
    "extracted",
    "converted",
    "ran",
    "fail",
    "error",
    "give_up",
    "src_lost",
    "src_restored",
)

# A backlog this deep already means the endpoint is not keeping up. Holding
# more would cost memory to deliver news that is no longer news.
MAX_QUEUED = 100
TIMEOUT = 10.0

_url: str | None = None
_events: frozenset[str] = frozenset()
_queue: queue.Queue[tuple[str, dict[str, Any]] | None] | None = None
_worker: threading.Thread | None = None
_dropped = 0
_failures = 0


def endpoint_host(url: str) -> str:
    """The part of a URL that is safe to print.

    Slack and Discord put the secret in the path, so the host is the most that
    can go in a log file without leaking the ability to post as you.
    """
    try:
        return urlparse(url).hostname or "?"
    except ValueError:
        return "?"


def configure(url: str | None, *, events: tuple[str, ...] = DEFAULT_EVENTS) -> None:
    """Point the webhook at `url`, or switch it off with None."""
    global _url, _events, _queue, _worker, _dropped, _failures
    _url = url or None
    _events = frozenset(events)
    _dropped = 0
    _failures = 0
    if _url is None:
        return
    if _queue is None:
        _queue = queue.Queue(maxsize=MAX_QUEUED)
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_drain, daemon=True, name="fw-webhook")
        _worker.start()


def stats() -> dict[str, int]:
    """What was not delivered, for the caller to report on the way out."""
    return {"dropped": _dropped, "failures": _failures}


def post(event: str, fields: dict[str, Any]) -> None:
    """Queue one event. Returns immediately, always."""
    global _dropped
    if _url is None or event not in _events or _queue is None:
        return
    try:
        _queue.put_nowait((event, dict(fields)))
    except queue.Full:
        # Dropping is the correct response: the alternative is making a file
        # copy wait on someone else's HTTP server.
        _dropped += 1


def _drain() -> None:
    global _failures
    while True:
        assert _queue is not None
        item = _queue.get()
        if item is None:
            return
        event, fields = item
        try:
            _send(event, fields)
        except Exception:
            # Deliberately broad: a webhook must never take a watcher down.
            # An unreachable endpoint is a lost notification, not a lost file.
            _failures += 1
        finally:
            _queue.task_done()


def _send(event: str, fields: dict[str, Any]) -> None:
    assert _url is not None
    body = json.dumps({"event": event, **fields}, default=str).encode()
    request = urllib.request.Request(
        _url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "folder-watch"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read(1)


def flush(timeout: float = 5.0) -> None:
    """Give queued events a chance to land before the process exits.

    Bounded: a watcher shutting down should not be held up by an endpoint that
    has stopped answering.
    """
    if _queue is None:
        return
    drained = threading.Event()

    def _wait() -> None:
        assert _queue is not None
        _queue.join()
        drained.set()

    threading.Thread(target=_wait, daemon=True).start()
    drained.wait(timeout)
