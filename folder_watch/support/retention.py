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
from pathlib import Path
from typing import Any

from folder_watch.presets import load_presets, preset_status
from folder_watch.support.paths import STATE_DIR

RETENTION_DAYS = 30
RECORD = STATE_DIR / "dead-presets.json"
# Which mount each preset's source was last seen on. Once the folder is gone
# nothing on disk tells an ejected volume from a deleted folder (hdiutil even
# takes away a mount point it made), so it is remembered while it can be seen.
VOLUMES = STATE_DIR / "preset-volumes.json"


def _dead(name: str, cfg: dict) -> bool:
    # Only a source deleted or renamed in place. An unmounted volume is "warn";
    # a bad destination or config is something to fix, not to collect.
    level, note = preset_status(cfg)
    if not (level == "error" and note.startswith("source missing")):
        if level == "ok":
            remember_volume(name, Path(str(cfg["src"])).expanduser())
        return False
    # Last seen on a volume that is not mounted now: ejected, not deleted.
    return not volume_ejected(name)


def volume_ejected(preset: str) -> bool:
    """The mount the preset's source was last seen on is not mounted now.
    False when it was never seen, or seen on one still mounted."""
    seen_on = _read_json(VOLUMES).get(preset)
    return bool(seen_on) and not os.path.ismount(str(seen_on))


def _mount_point(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if os.path.ismount(candidate):
            return candidate
    return Path("/")


def remember_volume(preset: str, src: Path) -> None:
    """Note the mount `src` sits on, while it exists. Never raises.

    shortcut: read-modify-write without a lock; two watchers starting at once
    can drop one note, which only means that preset is judged as before
    (missing = deleted) until it is seen again."""
    try:
        mount = str(_mount_point(src.resolve()))
        record = _read_json(VOLUMES)
        if record.get(preset) != mount:
            record[preset] = mount
            _write_json(VOLUMES, record)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ".json.tmp", so a crash mid-write leaves debris prune already knows.
    fd, tmp = tempfile.mkstemp(
        prefix=f"{path.stem}-", suffix=".json.tmp", dir=str(path.parent)
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _load() -> dict[str, float]:
    data = _read_json(RECORD)
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _save(record: dict[str, float]) -> None:
    _write_json(RECORD, record)


def review(now: float | None = None) -> list[tuple[str, float]]:
    """Every preset whose source is gone, oldest first, with when that was
    first seen. Updates the record as it goes: a new death is stamped now, and
    a source that came back, or a preset no longer stored, is dropped."""
    now = time.time() if now is None else now
    record = _load()
    dead = {
        name: record.get(name, now)
        for name, cfg in load_presets().items()
        if _dead(name, cfg)
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
    volumes = _read_json(VOLUMES)
    if volumes.pop(name, None) is not None:
        _write_json(VOLUMES, volumes)
