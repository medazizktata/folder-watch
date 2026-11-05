"""JSON presets under ~/.config/folder-watch.

There are no built-in presets. One used to be assembled at import time from a
.env file, which put a personal path and account address into source and, more
importantly, cannot work at all where the CLI has no .env to read. It was also
redundant: the stored preset defines every field and is merged on top anyway,
so the built-in only ever supplied a debounce_ms identical to the CLI default.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from folder_watch.support import audit
from folder_watch.support.log import emit
from folder_watch.support.paths import LEGACY_PRESETS, PRESETS_PATH
from folder_watch.support.webhook import InvalidEndpoint, check_url, endpoint_host

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
        "sound", "sound_name", "verify", "webhook", "reveal", "follow_moves",
        # action=route: [{"ext": [...], "action": "copy", "to": ...}, ...]
        "rules",
    }
)

# Keys with no flag on purpose: too structured for one, set in the preset
# (`presets edit NAME rules='[...]'`).
PRESET_ONLY = frozenset({"rules"})

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
    anchor = _nearest_existing(path)
    # The volume is there and the folder on it is not: that is a deletion.
    mounted = anchor != Path(anchor.anchor or "/") and os.path.ismount(anchor)
    container = _under_mount_container(path)
    if container is not None and not mounted:
        return "warn", f"{label} unreachable — volume not mounted under {container}"
    # shortcut: a volume mounted outside /Volumes and CloudStorage reads as
    # deleted once ejected; nothing left on disk tells the two apart.
    return "error", f"{label} missing — nearest existing folder is {anchor}"


def remove_preset(name: str) -> list[Path]:
    """Delete a preset from every file that defines it.

    Removing it from only the current file would leave a legacy copy still
    being merged in, so the preset would survive and the command would look
    broken. Returns the files actually changed.
    """
    changed: list[Path] = []
    with presets_locked():
        for path in preset_sources(name):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or name not in data:
                continue
            data.pop(name)
            _write_presets(path, data)
            changed.append(path)
    if changed:
        audit.record("preset_removed", preset=name, files=[str(p) for p in changed])
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
    problems = preset_problems(cfg)
    if problems:
        return "error", "; ".join(problems)
    src = Path(str(cfg["src"])).expanduser()
    if not src.is_dir():
        return _missing_note("source", src)

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


class PresetsUnreadable(RuntimeError):
    """presets.json exists but does not read as a JSON object."""


# Keys whose value is a credential in disguise. Endpoint URLs carry the token
# in the path (Slack and Discord both do), so a preset value printed or
# recorded verbatim hands out the ability to post as you.
SECRET_KEYS = frozenset({"webhook"})


def redacted(key: str, value: Any) -> Any:
    """A preset value as it is safe to show or record."""
    if key in SECRET_KEYS and value:
        return f"{endpoint_host(str(value))} (URL hidden)"
    return value


class InvalidPreset(ValueError):
    """A preset a watcher would misread, refused before it is stored."""


_lock_state = threading.local()


@contextlib.contextmanager
def presets_locked() -> Iterator[None]:
    """Hold the presets lock for a whole read, change and write.

    Every save reads the file, changes one preset and writes the lot back.
    Two processes doing that at once each wrote what they had read, so one
    change vanished (lost in 10 of 10 trials of four concurrent savers,
    including a real case: two presets following one renamed folder).
    Re-entrant within a thread, since edit_preset holds it around
    save_preset, and a second flock in one process would wait on itself."""
    depth = getattr(_lock_state, "depth", 0)
    if depth:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return
    PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PRESETS_PATH.parent / ".presets.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _lock_state.depth = 1
        try:
            yield
        finally:
            _lock_state.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_presets(path: Path, presets: dict[str, Any]) -> None:
    """Write through a temp file and a rename. Writing in place meant a crash
    mid-save left a half-written presets.json, the very file save_preset
    used to treat as empty.

    The rename goes to the real file behind a symlink, with its mode kept:
    renaming over the link itself turned a presets.json kept in a dotfiles
    folder into a private copy the dotfiles never saw again."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="presets-", suffix=".json.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(presets, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # Keep the file's mode (mkstemp's 0600 for a new one), but never let
        # others read a webhook URL: the token is part of it.
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        if any(isinstance(cfg, dict) and cfg.get(key)
               for cfg in presets.values() for key in SECRET_KEYS):
            mode &= 0o700
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_all() -> dict[str, Any]:
    """Every stored preset, or PresetsUnreadable: never an empty dict that a
    write would then put in place of a file it could not read."""
    if not PRESETS_PATH.exists():
        return {}
    try:
        text = PRESETS_PATH.read_text()
        # An empty file holds no presets to lose.
        loaded = json.loads(text) if text.strip() else {}
    except (OSError, json.JSONDecodeError) as e:
        raise PresetsUnreadable(
            f"{PRESETS_PATH} cannot be read ({e}); nothing was saved, so "
            "the presets in it are not lost. Fix or move the file, then retry."
        ) from e
    if not isinstance(loaded, dict):
        raise PresetsUnreadable(
            f"{PRESETS_PATH} does not hold a JSON object; nothing was saved."
        )
    return loaded


def save_preset(name: str, cfg: dict[str, Any]) -> None:
    """Add or replace one preset, keeping every other one.

    Raises PresetsUnreadable instead of starting from an empty file: when
    presets.json could not be parsed, saving one preset used to rewrite the
    file with that preset alone, silently dropping all the others.
    """
    problems = preset_problems(cfg)
    if problems:
        raise InvalidPreset(f"preset {name!r} not saved: " + "; ".join(problems))
    with presets_locked():
        presets = _read_all()
        before = presets.get(name)
        presets[name] = cfg
        _write_presets(PRESETS_PATH, presets)
    old = before if isinstance(before, dict) else {}
    audit.record(
        "preset_saved", preset=name, created=before is None,
        changes={
            k: [redacted(k, old.get(k)), redacted(k, cfg.get(k))]
            for k in sorted(set(old) | set(cfg)) if old.get(k) != cfg.get(k)
        },
    )


# How `presets edit KEY=VALUE` reads each value, mirroring the CLI flags.
_BOOL_KEYS = frozenset({
    "recursive", "no_clobber", "create_dest", "exclusive", "no_done_store",
    "verify", "reveal", "follow_moves", "overwrite", "sound",
})
_INT_KEYS = frozenset({
    "stable", "min_bytes", "debounce_ms", "quality", "width", "max_size",
})
_FLOAT_KEYS = frozenset({"poll", "settle", "retry_hours", "exec_timeout", "fps"})
_PATH_KEYS = frozenset({"src", "to", "convx_bin"})


_CHOICES = {
    "action": ("copy", "unzip", "exec", "convx", "route"),
    "mode": ("named", "same", "to"),
    "backend": ("event", "poll"),
}


def dest_inside_src(src: str | Path, to: str | Path) -> bool:
    """Whether `to` lies strictly under `src`: with recursive on, every copy
    lands back under the watch and is copied again, without limit."""
    try:
        dest = Path(to).expanduser().resolve()
        root = Path(src).expanduser().resolve()
    except OSError:
        return False
    return root in dest.parents


def preset_problems(cfg: dict[str, Any]) -> list[str]:
    """What is wrong with a preset's values, before anything runs or stores
    it. Only the config itself: a folder that does not exist yet is fine."""
    problems = []
    for key, allowed in _CHOICES.items():
        if key in cfg and cfg[key] not in allowed:
            problems.append(
                f"{key} must be one of {', '.join(allowed)}, not {cfg[key]!r}"
            )
    for key in sorted(_INT_KEYS | _FLOAT_KEYS):
        value = cfg.get(key)
        if value is None:
            continue
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key in _INT_KEYS:
            number = number and isinstance(value, int)
        if not number or not math.isfinite(value) or value < 0:
            kind = "a whole number" if key in _INT_KEYS else "a number"
            problems.append(f"{key} must be {kind}, 0 or more, not {value!r}")
        elif key == "poll" and value == 0:
            problems.append("poll must be more than 0 seconds")
        elif key == "quality" and value > 100:
            problems.append(f"quality must be 0-100, not {value!r}")
    for key in sorted(_BOOL_KEYS):
        if key in cfg and not isinstance(cfg[key], bool):
            problems.append(f"{key} must be true or false, not {cfg[key]!r}")
    for key in ("src", "to", "cmd", "convx_bin", "sound_name"):
        if key in cfg and cfg[key] is not None and (
            not isinstance(cfg[key], str) or not cfg[key].strip()
        ):
            problems.append(f"{key} must be a non-empty string")
    if "ext" in cfg and not (
        isinstance(cfg["ext"], list) and all(isinstance(e, str) for e in cfg["ext"])
    ):
        problems.append("ext must be a list of extensions")
    if cfg.get("webhook"):
        try:
            check_url(cfg["webhook"])
        except InvalidEndpoint as e:
            problems.append(str(e))
    if problems:
        return problems

    action = cfg.get("action", "copy")
    if not cfg.get("src"):
        problems.append("no src")
    if action == "route":
        problems += _rule_problems(cfg)
    if action == "copy" and not cfg.get("to"):
        problems.append("copy requires 'to'")
    if action == "unzip" and cfg.get("mode") == "to" and not cfg.get("to"):
        problems.append("mode=to requires 'to'")
    if action == "exec" and not cfg.get("cmd"):
        problems.append("exec requires 'cmd'")
    if cfg.get("recursive") and cfg.get("src") and cfg.get("to") \
            and dest_inside_src(cfg["src"], cfg["to"]):
        problems.append("'to' is inside 'src' and recursive is on, so every copy "
                        "would be copied again")
    return problems


# What a rule may set for its own files; the rest comes from the preset.
RULE_KEYS = frozenset({
    "ext", "action", "to", "mode", "cmd", "exec_timeout",
    "no_clobber", "create_dest", "verify",
})


def _rule_problems(cfg: dict[str, Any]) -> list[str]:
    """Each rule is checked as the preset it amounts to for its own files, so
    every check above (a copy needs 'to', a destination inside a recursive
    source) applies to it unchanged."""
    rules = cfg.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["route needs 'rules': a list of {ext, action, ...}"]
    base = {k: v for k, v in cfg.items() if k not in ("rules", "action", "ext")}
    problems = []
    for n, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            problems.append(f"rule {n} is not an object")
            continue
        unknown = sorted(set(rule) - RULE_KEYS)
        if unknown:
            problems.append(f"rule {n}: unknown key(s) {', '.join(unknown)}")
        if "ext" not in rule:
            # Left out, it matched every file and hid the rules after it.
            problems.append(f"rule {n} needs 'ext' (use [] to take every file)")
        if rule.get("action") not in ("copy", "unzip", "exec"):
            problems.append(f"rule {n}: action must be copy, unzip or exec, "
                            f"not {rule.get('action')!r}")
            continue
        problems += [f"rule {n}: {p}" for p in preset_problems({**base, **rule})]
    catch_all = [n for n, r in enumerate(rules, 1)
                 if isinstance(r, dict) and r.get("ext") == []]
    if catch_all and catch_all[0] != len(rules):
        problems.append(f"rule {catch_all[0]} matches every file, so the rules after "
                        "it are never reached; move it last")
    return problems


def parse_setting(key: str, raw: str) -> Any:
    """The stored form of `KEY=VALUE` as typed on a command line."""
    if key not in PRESET_KEYS:
        known = ", ".join(sorted(PRESET_KEYS))
        raise ValueError(f"unknown key {key!r}; known: {known}")
    if key in _BOOL_KEYS:
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        raise ValueError(f"{key} takes true or false, not {raw!r}")
    if key in _INT_KEYS:
        return int(raw)
    if key in _FLOAT_KEYS:
        return float(raw)
    if key == "rules":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"rules takes JSON, as in rules='[{{\"ext\": [\"zip\"], "
                             f"\"action\": \"unzip\"}}]' ({e})") from None
    if key == "ext":
        return [e.strip().lstrip(".").lower() for e in raw.split(",") if e.strip()]
    if key in _PATH_KEYS:
        if not raw.strip():
            # Path("").resolve() is the current folder, which is never meant.
            raise ValueError(f"{key} needs a path; use --unset {key} to remove it")
        # Absolute, as a LaunchAgent's PathState needs.
        return str(Path(raw).expanduser().resolve())
    return raw


def edit_preset(
    name: str, settings: dict[str, Any], unset: list[str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Change some keys of a stored preset and keep the rest. Values must
    already be parsed. Returns (before, after); raises LookupError for an
    unknown preset and ValueError for an unknown key."""
    for key in [*settings, *(unset or [])]:
        if key not in PRESET_KEYS:
            raise ValueError(f"unknown key {key!r}")
    # Held across the read and the save, so the change lands on what is there.
    with presets_locked():
        stored = _read_all().get(name)
        if not isinstance(stored, dict):
            raise LookupError(f"no preset {name!r}")
        before = dict(stored)
        after = {**before, **settings}
        for key in unset or []:
            after.pop(key, None)
        save_preset(name, after)
    return before, after
