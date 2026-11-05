"""route: one watched folder, a different action per kind of file.

A preset's `rules` are tried in order and the first whose extensions match a
file decides what happens to it (`"ext": []` matches everything, so it goes
last as the catch-all). The watcher itself only picks up files some rule
matches: the union of the rules' extensions is its --ext.
"""
from __future__ import annotations

from pathlib import Path

from folder_watch.actions.base import Action
from folder_watch.core.util import match_ext


class RouteAction(Action):
    name = "route"

    def __init__(self, rules: list[tuple[list[str], Action]]) -> None:
        if not rules:
            raise ValueError("route needs at least one rule")
        self.rules = rules

    def rule_for(self, path: Path) -> Action | None:
        for exts, action in self.rules:
            if match_ext(path.name, exts):
                return action
        return None

    def run(self, path: Path) -> str:
        action = self.rule_for(path)
        # The watcher's --ext is the union of the rules', so this is a file
        # that changed name between the scan and now.
        return action.run(path) if action is not None else "skipped"

    def describe(self) -> str:
        return "route " + "; ".join(
            f"{','.join(exts) or '*'} -> {action.describe()}"
            for exts, action in self.rules
        )

    def done_dest(self, src: Path) -> Path | None:
        action = self.rule_for(src)
        return action.done_dest(src) if action is not None else None

    def destination_ready(self) -> bool:
        # All of them: one drive away is reason enough to hold the retries,
        # rather than fail each of its queued files again on every pass.
        return all(action.destination_ready() for _, action in self.rules)

    def output_exists(self, src: Path) -> bool | None:
        action = self.rule_for(src)
        return action.output_exists(src) if action is not None else None

    @property
    def reveal_on_done(self) -> bool:
        return any(action.reveal_on_done for _, action in self.rules)

    @reveal_on_done.setter
    def reveal_on_done(self, value: bool) -> None:
        for _, action in self.rules:
            action.reveal_on_done = value
