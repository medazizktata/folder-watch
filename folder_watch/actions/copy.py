"""Copy action — atomic copy + optional notify."""
from __future__ import annotations

from pathlib import Path

from folder_watch.actions.base import Action
from folder_watch.support.copy_io import atomic_copy
from folder_watch.support.log import emit
from folder_watch.support.notify import human_size, notify
from folder_watch.support.paths import DestUnavailable, ensure_dest_dir


class CopyAction(Action):
    name = "copy"
    default_ext = "mp4"
    default_min_bytes = 1_000_000

    def __init__(
        self,
        dest: Path,
        *,
        notify_on_done: bool = True,
        retries: int = 5,
        dry_run: bool = False,
        create_dest: bool = False,
    ) -> None:
        self.dest = dest.expanduser()
        self.notify_on_done = notify_on_done
        self.retries = retries
        self.dry_run = dry_run
        self.create_dest = create_dest

    def describe(self) -> str:
        bits = [f"copy -> {self.dest}"]
        if self.notify_on_done:
            bits.append("notify")
        if self.dry_run:
            bits.append("dry-run")
        return " ".join(bits)

    def done_dest(self, src: Path) -> Path | None:
        return self.dest / src.name

    def output_exists(self, src: Path) -> bool | None:
        dest = self.dest / src.name
        try:
            return dest.is_file() and dest.stat().st_size == src.stat().st_size
        except OSError:
            return None

    def run(self, path: Path) -> str:
        dest = self.dest / path.name
        size = path.stat().st_size
        if dest.is_file() and dest.stat().st_size == size:
            return "skip"
        if self.dry_run:
            emit("would_copy", name=path.name, size=size, to=dest)
            return "would_copy"
        try:
            ensure_dest_dir(self.dest, create_parents=self.create_dest)
        except DestUnavailable as e:
            # Never mark done — the file must be retried once the volume is back.
            emit("fail", name=path.name, reason="dest_unavailable", err=str(e))
            if self.notify_on_done:
                notify("folder-watch destination missing", f"{path.name} not copied")
            return "fail"
        try:
            atomic_copy(path, dest, retries=self.retries)
        except OSError as e:
            emit("fail", name=path.name, reason="copy_error", err=str(e))
            if self.notify_on_done:
                notify("folder-watch copy failed", path.name, reveal=dest)
            return "fail"
        if not dest.is_file() or dest.stat().st_size != size:
            emit(
                "fail",
                name=path.name,
                reason="size_mismatch",
                size=size,
                wrote=dest.stat().st_size if dest.is_file() else 0,
            )
            if self.notify_on_done:
                notify("folder-watch copy failed", path.name, reveal=dest)
            return "fail"
        if self.notify_on_done:
            notify(
                "folder-watch copied",
                f"{path.name} ({human_size(size)}) → {self.dest.name}",
                reveal=dest,
            )
        return "copied"

