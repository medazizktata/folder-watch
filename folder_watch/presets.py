"""JSON presets under ~/.config/folder-watch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from folder_watch.support.paths import CONFIG_DIR, LEGACY_PRESETS, PRESETS_PATH

DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "la-chaise-v1": {
        "action": "copy",
        "src": "/Users/MAC/Desktop/project-batch2/Output",
        "to": (
            "/Users/MAC/Library/CloudStorage/"
            "GoogleDrive-you@example.com/"
            ".shortcut-targets-by-id/SHORTCUT_ID/"
            "Client/Project/Batch 2/Output/Videos /V2"
        ),
        "ext": ["mp4"],
        "poll": 30,
        "stable": 3,
        "min_bytes": 1_000_000,
        "backend": "event",
        "debounce_ms": 500,
    }
}

def load_presets() -> dict[str, dict[str, Any]]:
    merged = dict(DEFAULT_PRESETS)
    for path in (LEGACY_PRESETS, PRESETS_PATH):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            norm = dict(cfg)
            if "dest" in norm and "to" not in norm:
                norm["to"] = norm.pop("dest")
            if "action" not in norm:
                norm["action"] = "copy"
            merged[name] = {**merged.get(name, {}), **norm}
    return merged


def save_preset(name: str, cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    presets: dict[str, Any] = {}
    if PRESETS_PATH.is_file():
        try:
            presets = json.loads(PRESETS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            presets = {}
    presets[name] = cfg
    PRESETS_PATH.write_text(json.dumps(presets, indent=2) + "\n")
