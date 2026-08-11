"""Built-in actions registry."""
from __future__ import annotations

from folder_watch.actions.base import Action
from folder_watch.actions.copy import CopyAction
from folder_watch.actions.unzip import UnzipAction

ACTIONS: dict[str, type[Action]] = {
    "copy": CopyAction,
    "unzip": UnzipAction,
}
ACTION_NAMES = ("copy", "unzip", "convx")

__all__ = [
    "ACTIONS",
    "ACTION_NAMES",
    "Action",
    "CopyAction",
    "UnzipAction",
]
