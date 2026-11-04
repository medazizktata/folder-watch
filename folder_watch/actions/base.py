"""Action base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from folder_watch.support.notify import reveal_in_finder


class Action(ABC):
    name: str
    default_ext: str = "*"
    default_min_bytes: int = 1
    # --reveal: open Finder on each result as it lands, no click needed.
    reveal_on_done: bool = False

    def reveal(self, path: Path) -> None:
        if self.reveal_on_done:
            reveal_in_finder(path)

    @abstractmethod
    def run(self, path: Path) -> str:
        """Return action result label."""

    def describe(self) -> str:
        return self.name

    def done_dest(self, src: Path) -> Path | None:
        """Destination path for done-state key (scheme B), if any."""
        return None

    def done_mode(self) -> str | None:
        return None

    def destination_ready(self) -> bool:
        """Is the destination reachable right now?

        Lets the retry loop ask once per scan instead of failing every queued
        file individually when a volume is away — 500 files behind an unmounted
        drive should cost one probe, not 500 attempts and 500 log lines.
        """
        return True

    def output_exists(self, src: Path) -> bool | None:
        """Has this file's output already been produced?

        Baseline seeding skips whatever is already sitting in the folder at
        startup, on the assumption it's old news. That assumption is never
        checked: a file that was never processed looks exactly like one that
        was, and gets skipped forever. This is what tells them apart.

        None means the action can't tell.
        """
        return None
