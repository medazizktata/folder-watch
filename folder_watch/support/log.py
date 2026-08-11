"""Logging + event emit."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("folder_watch")

_JSON_MODE = False
_VERBOSE = False


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
        else logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    )
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    log.addHandler(err)

    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(lvl)
        log.addHandler(fh)


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


def emit(event: str, **fields: Any) -> None:
    payload = {"event": event, **{k: _jsonable(v) for k, v in fields.items()}}
    if _JSON_MODE:
        line = json.dumps(payload, ensure_ascii=False)
    else:
        bits = [event] + [f"{k}={v}" for k, v in fields.items()]
        line = " ".join(bits)
    print(line, flush=True)
    log.info("%s", line)


def emit_debug(event: str, **fields: Any) -> None:
    if not _VERBOSE:
        return
    emit(event, **fields)


# re-export helpers used by state/pidfile
jsonable = _jsonable
