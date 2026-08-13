"""JSON presets under ~/.config/folder-watch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from folder_watch.support.env import env
from folder_watch.support.log import emit
from folder_watch.support.paths import CONFIG_DIR, LEGACY_PRESETS, PRESETS_PATH

# Every key a preset may carry. Nothing derives this automatically from the
# argument parser, so a flag added without touching cli.py's preset block is
# simply ignored when it appears in a preset — silently, which is the part
# that bites. test_preset_coverage keeps this honest.
PRESET_KEYS = frozenset(
    {
        "action", "src", "to", "mode",
        "ext", "min_bytes", "recursive",
        "poll", "stable", "settle", "backend", "debounce_ms",
        "no_clobber", "create_dest", "exclusive", "no_done_store", "retry_hours",
        "format", "convx_preset", "convx_bin", "quality", "width", "fps",
        "max_size", "overwrite",
    }
)

# Per-invocation by nature: persisting them would mean a preset that always
# runs once, or always previews, or never writes its log where you asked.
NOT_PERSISTED = frozenset(
    {
        "help", "version", "preset", "save_preset",
        "list_presets", "list_actions",
        "once", "catch_up", "dry_run", "pending",
        # Arguably these could be persisted; they are a deliberate omission
        # rather than an oversight, and the LaunchAgent passes --log itself.
        "log", "log_level", "json", "verbose", "notify", "retries",
    }
)


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
            # A key the loader does not read does nothing, and --list-presets
            # happily echoes it back — so a misspelled "no_clober", or a flag
            # that was never wired up, looks like working configuration.
            unknown = sorted(set(norm) - PRESET_KEYS)
            if unknown:
                emit(
                    "warn",
                    reason="unknown_preset_keys",
                    preset=name,
                    keys=",".join(unknown),
                    note="ignored — not settable from a preset",
                )
            merged[name] = {**merged.get(name, {}), **norm}
    return merged


def _mount_containers() -> tuple[Path, ...]:
    """Directories whose children are mounts rather than ordinary folders."""
    return (Path("/Volumes"), Path.home() / "Library" / "CloudStorage")


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return Path(path.anchor or "/")


def _missing_note(label: str, path: Path) -> tuple[str, str]:
    """Say what is actually true instead of guessing why.

    Checking only the immediate parent got this wrong: for a deleted project
    folder the parent is missing too, so it reported "volume not mounted"
    about a path sitting plainly under an existing ~/Desktop. Walking up to
    the nearest folder that does exist is both accurate and the thing you
    need to know to fix it.
    """
    anchor = _nearest_existing(path)
    if anchor in _mount_containers():
        return "warn", f"{label} unreachable — volume not mounted under {anchor}"
    return "error", f"{label} missing — nearest existing folder is {anchor}"


def preset_status(cfg: dict[str, Any]) -> tuple[str, str]:
    """Health of a preset, for display only. Never touches anything.

    The tool cannot tell "this project is over" from "the drive is unplugged",
    and its own runtime already assumes a missing folder is temporary —
    src_lost/src_restored exist for exactly that. So this reports and leaves
    the decision alone.

    The distinction it CAN make is worth surfacing, because the two point in
    opposite directions: if the parent still exists someone deleted or renamed
    the folder and you need to act; if the whole path is gone it is probably an
    unmounted volume and you need to do nothing.

    Returns (level, note); level is "ok", "warn" or "error".
    """
    action = cfg.get("action", "copy")
    if action not in ("copy", "unzip", "convx"):
        return "error", f"unknown action {action!r}"

    raw_src = cfg.get("src")
    if not raw_src:
        return "error", "no src"
    src = Path(str(raw_src)).expanduser()
    if not src.is_dir():
        return _missing_note("source", src)

    if action == "copy" and not cfg.get("to"):
        return "error", "copy requires 'to'"
    if action == "unzip" and cfg.get("mode") == "to" and not cfg.get("to"):
        return "error", "mode=to requires 'to'"

    raw_to = cfg.get("to")
    if raw_to:
        dest = Path(str(raw_to)).expanduser()
        if not dest.is_dir() and not dest.parent.is_dir():
            return _missing_note("destination", dest)
    return "ok", ""


def load_raw_preset(name: str) -> dict[str, Any] | None:
    """A preset exactly as stored, without built-in defaults merged in.

    save_preset needs to know whether it is creating or modifying, and
    load_presets() cannot answer that — it folds DEFAULT_PRESETS in, so a
    name that exists only as a built-in would look like a stored preset.
    """
    if not PRESETS_PATH.is_file():
        return None
    try:
        data = json.loads(PRESETS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stored = data.get(name)
    return dict(stored) if isinstance(stored, dict) else None


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
