"""JSON presets under ~/.config/folder-watch.

There are no built-in presets. One used to be assembled at import time from a
.env file, which put a personal path and account address into source and, more
importantly, cannot work at all where the CLI has no .env to read. It was also
redundant: the stored preset defines every field and is merged on top anyway,
so the built-in only ever supplied a debounce_ms identical to the CLI default.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
        "max_size", "overwrite", "passthru",
        "cmd", "exec_timeout",
        # Preset-settable, unlike notify: the watchers that run unattended are
        # exactly the ones you want to hear, and they are started from a
        # preset with no other flags.
        "sound", "sound_name",
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
        "list_sounds",
    }
)


def load_presets() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
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


def _under_mount_container(path: Path) -> Path | None:
    """The mount container this path sits under, by shape rather than existence.

    Checking whether the nearest EXISTING ancestor is a container made the
    answer depend on the machine: with Google Drive installed
    ~/Library/CloudStorage exists and the path reads as unmounted, without it
    the walk lands on ~/Library and the same path reads as deleted. The path
    shape is the durable signal — a CloudStorage path with no CloudStorage
    directory at all is still an unmounted drive, not a deleted folder.
    """
    for container in _mount_containers():
        if path == container:
            continue
        try:
            path.relative_to(container)
        except ValueError:
            continue
        return container
    return None


def _missing_note(label: str, path: Path) -> tuple[str, str]:
    """Say what is actually true instead of guessing why.

    Checking only the immediate parent got this wrong: for a deleted project
    folder the parent is missing too, so it reported "volume not mounted"
    about a path sitting plainly under an existing ~/Desktop. Naming the
    nearest folder that does exist is both accurate and the thing you need in
    order to fix it.
    """
    container = _under_mount_container(path)
    if container is not None:
        return "warn", f"{label} unreachable — volume not mounted under {container}"
    anchor = _nearest_existing(path)
    return "error", f"{label} missing — nearest existing folder is {anchor}"


def remove_preset(name: str) -> list[Path]:
    """Delete a preset from every file that defines it.

    Removing it from only the current file would leave a legacy copy still
    being merged in, so the preset would survive and the command would look
    broken. Returns the files actually changed.
    """
    changed: list[Path] = []
    for path in preset_sources(name):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or name not in data:
            continue
        data.pop(name)
        path.write_text(json.dumps(data, indent=2) + "\n")
        changed.append(path)
    return changed


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
    if action not in ("copy", "unzip", "exec", "convx"):
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
    if action == "exec" and not cfg.get("cmd"):
        return "error", "exec requires 'cmd'"

    raw_to = cfg.get("to")
    if raw_to:
        dest = Path(str(raw_to)).expanduser()
        if not dest.is_dir() and not dest.parent.is_dir():
            return _missing_note("destination", dest)
    return "ok", ""


def preset_sources(name: str) -> list[Path]:
    """Every file that defines this preset, in merge order.

    There is more than one: a pre-rename legacy path is still read, so a
    preset can be defined twice and removing it from one file leaves it
    working. `presets forget` has to say so rather than appear to fail.
    """
    found: list[Path] = []
    for path in (LEGACY_PRESETS, PRESETS_PATH):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and name in data:
            found.append(path)
    return found


def load_raw_preset(name: str) -> dict[str, Any] | None:
    """A preset exactly as stored, without built-in defaults merged in.

    save_preset needs to know whether it is creating or modifying, and
    Kept separate from load_presets() so "is this stored?" stays a distinct
    question from "what will this resolve to?".
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
