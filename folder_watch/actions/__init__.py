"""Built-in actions registry."""
from __future__ import annotations

from folder_watch.actions.base import Action
from folder_watch.actions.copy import CopyAction
from folder_watch.actions.exec_cmd import ExecAction
from folder_watch.actions.unzip import UnzipAction

ACTIONS: dict[str, type[Action]] = {
    "copy": CopyAction,
    "unzip": UnzipAction,
    "exec": ExecAction,
}
# convx is here but not in ACTIONS: it wraps another tool rather than running
# inside the settle loop, so the CLI dispatches it before an Action is built.
ACTION_NAMES = ("copy", "unzip", "exec", "convx")

__all__ = [
    "ACTIONS",
    "ACTION_NAMES",
    "Action",
    "CopyAction",
    "ExecAction",
    "UnzipAction",
]
