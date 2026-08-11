"""Action base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Action(ABC):
    name: str
    default_ext: str = "*"
    default_min_bytes: int = 1

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
