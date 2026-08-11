"""Minimal .env reader — local paths and account identifiers, kept out of git.

No dependency and no shell semantics: no interpolation, no command
substitution, no multi-line values. `KEY=value`, one per line, `#` comments,
optional surrounding quotes, optional `export ` prefix.

A real environment variable always wins over the file, so a one-off override
works the way you'd expect:

    FOLDER_WATCH_DRIVE_EMAIL=other@example.com folder-watch --preset ...

Searched in order, first file found wins:
    $FOLDER_WATCH_ENV
    ~/.config/folder-watch/.env
    <repo root>/.env
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from folder_watch.support.paths import CONFIG_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]


def env_paths() -> list[Path]:
    override = os.environ.get("FOLDER_WATCH_ENV")
    paths = [Path(override).expanduser()] if override else []
    paths.append(CONFIG_DIR / ".env")
    paths.append(REPO_ROOT / ".env")
    return paths


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@lru_cache(maxsize=1)
def load_env() -> dict[str, str]:
    for path in env_paths():
        try:
            if path.is_file():
                return parse_env(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return {}


def env(name: str, default: str | None = None) -> str | None:
    """Real environment first, then the .env file, then `default`."""
    value = os.environ.get(name)
    if value:
        return value
    return load_env().get(name) or default
