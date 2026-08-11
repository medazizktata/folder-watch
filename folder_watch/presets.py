"""JSON presets under ~/.config/folder-watch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from folder_watch.support.env import env
from folder_watch.support.paths import CONFIG_DIR, LEGACY_PRESETS, PRESETS_PATH


def drive_root() -> Path | None:
    """Base of the Google Drive CloudStorage mount, from .env.

    Returns None when unconfigured — the account address and shared-drive id
    are local details, so they live in .env rather than in this file.
    """
    email = env("FOLDER_WATCH_DRIVE_EMAIL")
    if not email:
        return None
    root = Path.home() / "Library" / "CloudStorage" / f"GoogleDrive-{email}"
    shortcut = env("FOLDER_WATCH_DRIVE_SHORTCUT_ID")
    return root / ".shortcut-targets-by-id" / shortcut if shortcut else root


def _la_chaise() -> dict[str, Any] | None:
    src = env("FOLDER_WATCH_LA_CHAISE_SRC")
    rel = env("FOLDER_WATCH_LA_CHAISE_DEST")
    root = drive_root()
    if not src or not rel or root is None:
        return None
    return {
        "action": "copy",
        "src": src,
        "to": str(root / rel),
        "ext": ["mp4"],
        "poll": 30,
        "stable": 3,
        "min_bytes": 1_000_000,
        "backend": "event",
        "debounce_ms": 500,
    }


def _defaults() -> dict[str, dict[str, Any]]:
    """Built-ins, omitted entirely when .env doesn't configure them.

    Anything in ~/.config/folder-watch/presets.json overrides these, so a
    missing .env is not a problem for presets you've already saved.
    """
    out: dict[str, dict[str, Any]] = {}
    la_chaise = _la_chaise()
    if la_chaise is not None:
        out["la-chaise-v1"] = la_chaise
    return out


DEFAULT_PRESETS: dict[str, dict[str, Any]] = _defaults()

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
