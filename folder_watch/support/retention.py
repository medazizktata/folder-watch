"""When a preset whose source folder is gone may be removed.

The tool still cannot tell "this project is over" from "the folder will be
back", so nothing here decides on one look. It records when each preset's
source was first seen deleted or renamed, and only a preset that has stayed
that way for RETENTION_DAYS is due. A source on an unmounted volume never
counts: that is the case where waiting is right.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from folder_watch.presets import load_presets, preset_status
from folder_watch.support.paths import STATE_DIR

RETENTION_DAYS = 30
RECORD = STATE_DIR / "dead-presets.json"


def _dead(cfg: dict) -> bool:
    # Only a source deleted or renamed in place. An unmounted volume is "warn";
    # a bad destination or config is something to fix, not to collect.
    level, note = preset_status(cfg)
    return level == "error" and note.startswith("source missing")


def _load() -> dict[str, float]:
    try:
        data = json.loads(RECORD.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _save(record: dict[str, float]) -> None:
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    # ".json.tmp", so a crash mid-write leaves debris prune already knows.
    fd, tmp = tempfile.mkstemp(
        prefix="dead-presets-", suffix=".json.tmp", dir=str(RECORD.parent)
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, RECORD)


def review(now: float | None = None) -> list[tuple[str, float]]:
    """Every preset whose source is gone, oldest first, with when that was
    first seen. Updates the record as it goes: a new death is stamped now, and
    a source that came back, or a preset no longer stored, is dropped."""
    now = time.time() if now is None else now
    record = _load()
    dead = {
        name: record.get(name, now)
        for name, cfg in load_presets().items()
        if _dead(cfg)
    }
    if dead != record:
        _save(dead)
    return sorted(dead.items(), key=lambda item: item[1])


def is_due(since: float, now: float) -> bool:
    return now - since >= RETENTION_DAYS * 86400


def forget(name: str) -> None:
    record = _load()
    if record.pop(name, None) is not None:
        _save(record)
