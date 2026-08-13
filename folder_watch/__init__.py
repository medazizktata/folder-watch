"""folder-watch — watch a folder; run a pluggable action when files finish writing."""

from __future__ import annotations

from folder_watch._version import __version__
from folder_watch.actions import ACTION_NAMES, ACTIONS, Action, CopyAction, UnzipAction
from folder_watch.actions.convx import build_convx_watch_cmd, run_convx_watch
from folder_watch.cli import main
from folder_watch.core.util import is_open, match_ext, parse_exts, should_watch_name
from folder_watch.core.watch import apply_action, run_watch, settle_step
from folder_watch.presets import load_presets, save_preset
from folder_watch.support.notify import human_size, notify, reveal_in_finder
from folder_watch.support.paths import CONFIG_DIR, LEGACY_PRESETS, PRESETS_PATH

__all__ = [
    "ACTIONS",
    "ACTION_NAMES",
    "CONFIG_DIR",
    "LEGACY_PRESETS",
    "PRESETS_PATH",
    "Action",
    "CopyAction",
    "UnzipAction",
    "__version__",
    "apply_action",
    "build_convx_watch_cmd",
    "human_size",
    "is_open",
    "load_presets",
    "main",
    "match_ext",
    "notify",
    "parse_exts",
    "reveal_in_finder",
    "run_convx_watch",
    "run_watch",
    "save_preset",
    "settle_step",
    "should_watch_name",
]
