"""Logging + event emit."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from folder_watch.support import audit, webhook

log = logging.getLogger("folder_watch")

_JSON_MODE = False
_VERBOSE = False
_TO_STDOUT = True

# --log rotates here. Unrotated, a long-lived watcher's log grew for as long
# as it ran.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3


def setup_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    json_mode: bool = False,
) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    log.handlers.clear()
    log.setLevel(lvl)
    log.propagate = False

    fmt = (
        logging.Formatter("%(message)s")
        if json_mode
        # The date too: a time alone cannot say which day a line is from, so
        # nothing could count a week of results from the log.
        else logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    log.addHandler(err)

    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(lvl)
        log.addHandler(fh)

    # Under launchd stdout is a file as well (the .out log), so with --log
    # every event was written twice. A terminal still gets them.
    global _TO_STDOUT
    _TO_STDOUT = log_file is None or sys.stdout.isatty()


def set_json_mode(on: bool) -> None:
    global _JSON_MODE
    _JSON_MODE = on


def set_verbose(on: bool) -> None:
    global _VERBOSE
    _VERBOSE = on


def is_verbose() -> bool:
    return _VERBOSE


def _jsonable(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    return v


def _escape_table() -> dict[int, str]:
    """Every control character, not a hand-picked few.

    Escaping only \n and \r left ESC through, so a filename could still emit
    terminal control sequences into a log someone later cats.
    """
    table: dict[int, str] = {
        ord("\\"): "\\\\",
        ord('"'): '\\"',
        ord("\n"): "\\n",
        ord("\r"): "\\r",
        ord("\t"): "\\t",
    }
    for code in range(0x20):
        table.setdefault(code, f"\\x{code:02x}")
    table[0x7F] = "\\x7f"
    return table


_ESCAPES = _escape_table()


def _plain(value: Any) -> str:
    """One field of the plain-text format, safe to render.

    A filename containing a newline could otherwise emit a second, entirely
    well-formed line — `copied name=FAKE.mp4 size=999` — from a `progress`
    event. Control characters are escaped, and anything still ambiguous gets
    quoted so field boundaries survive. --json was never affected.
    """
    text = str(value).translate(_ESCAPES)
    if text == "" or any(c in text for c in ' \t="'):
        return '"' + text + '"'
    return text


# Events that go into the audit trail as well as the log: what happened to
# a file, and what happened to a watched folder.
AUDITED = frozenset({"copied", "extracted", "ran", "fail", "give_up",
                     "src_followed", "src_not_followed"})
AUDITED_REASONS = frozenset(
    {"src_moved", "follow_refused", "presets_unreadable", "preset_invalid"}
)


def emit(event: str, **fields: Any) -> None:
    payload = {"event": event, **{k: _jsonable(v) for k, v in fields.items()}}
    if event in AUDITED:
        audit.record(event, **fields)
    elif event in ("error", "warn") and fields.get("reason") in AUDITED_REASONS:
        audit.record(str(fields["reason"]), **fields)
    # Fire-and-forget; returns immediately whether or not anything is listening.
    webhook.post(event, payload)
    if _JSON_MODE:
        stamped = {"ts": datetime.now().isoformat(timespec="seconds"), **payload}
        line = json.dumps(stamped, ensure_ascii=False)
    else:
        bits = [event] + [f"{k}={_plain(v)}" for k, v in fields.items()]
        line = " ".join(bits)
    if _TO_STDOUT:
        print(line, flush=True)
    log.info("%s", line)


def emit_debug(event: str, **fields: Any) -> None:
    if not _VERBOSE:
        return
    emit(event, **fields)


# re-export helpers used by state/pidfile
jsonable = _jsonable
