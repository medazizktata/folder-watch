"""Copy action — atomic copy + optional notify."""
from __future__ import annotations

import errno
from pathlib import Path

from folder_watch.actions.base import Action
from folder_watch.support.copy_io import atomic_copy
from folder_watch.support.log import emit
from folder_watch.support.notify import human_size, notify
from folder_watch.support.paths import DestUnavailable, ensure_dest_dir


def _same_version(src: Path, dest: Path) -> bool:
    """True when dest already holds this exact version of src.

    shutil.copy2 preserves mtime, so matching (size, mtime) means we copied
    this content. Size alone silently misses a same-length rewrite.
    """
    a, b = src.stat(), dest.stat()
    return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns


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
        src_root: Path | None = None,
    ) -> None:
        self.dest = dest.expanduser()
        self.notify_on_done = notify_on_done
        self.retries = retries
        self.dry_run = dry_run
        self.create_dest = create_dest
        self.src_root = src_root.expanduser() if src_root else None

    def describe(self) -> str:
        bits = [f"copy -> {self.dest}"]
        if self.notify_on_done:
            bits.append("notify")
        if self.dry_run:
            bits.append("dry-run")
        return " ".join(bits)

    def _dest_for(self, src: Path) -> Path:
        """Mirror the source tree under dest.

        Using only src.name flattens a --recursive watch: two files called
        final.mp4 in different subfolders both resolve here, the second
        silently overwrites the first, and both report "copied". Without
        --recursive every file sits directly in src_root, so the relative
        path is just the name and this is a no-op.
        """
        if self.src_root is not None:
            try:
                return self.dest / src.resolve().relative_to(self.src_root.resolve())
            except (ValueError, OSError):
                pass
        return self.dest / src.name

    def destination_ready(self) -> bool:
        try:
            ensure_dest_dir(self.dest, create_parents=self.create_dest)
        except (DestUnavailable, OSError):
            return False
        return True

    def done_dest(self, src: Path) -> Path | None:
        return self._dest_for(src)

    def output_exists(self, src: Path) -> bool | None:
        """Is there an output for this file?

        Deliberately size-only, unlike run()'s skip test. This answers "has
        anything been produced" for --pending and the baseline probe gate; a
        destination populated by rsync or by hand still counts. run() needs
        the stricter mtime check because it decides whether to re-copy.
        """
        dest = self._dest_for(src)
        try:
            return dest.is_file() and dest.stat().st_size == src.stat().st_size
        except OSError:
            return None

    def run(self, path: Path) -> str:
        dest = self._dest_for(path)
        size = path.stat().st_size
        # Size alone can't tell a re-export from the copy already there:
        # muxers rewrite a moov atom in place without changing length, and
        # text edits often preserve it. copy2 preserves mtime, so an equal
        # (size, mtime) pair means this exact version is already at dest.
        if dest.is_file() and _same_version(path, dest):
            return "skip"
        if self.dry_run:
            emit("would_copy", name=path.name, size=size, to=dest)
            return "would_copy"
        try:
            ensure_dest_dir(self.dest, create_parents=self.create_dest)
            # Subdirectories under a destination we've already vetted are
            # ours to create — this is not the unmounted-volume case.
            if dest.parent != self.dest:
                dest.parent.mkdir(parents=True, exist_ok=True)
        except DestUnavailable as e:
            # Never mark done — the file must be retried once the volume is back.
            # Transient: the volume comes back. Queued, not dropped.
            emit("fail", name=path.name, reason="dest_unavailable", err=str(e))
            if self.notify_on_done:
                notify("folder-watch destination missing", f"{path.name} not copied")
            return "retry"
        try:
            atomic_copy(path, dest, retries=self.retries)
        except OSError as e:
            emit("fail", name=path.name, reason="copy_error", err=str(e))
            if self.notify_on_done:
                notify("folder-watch copy failed", path.name, reveal=dest)
            # A read-only or forbidden destination will not fix itself; queueing
            # it would retry a certainty for three days. Anything else (network
            # volume, transient IO) is worth another go.
            if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                return "fail"
            return "retry"
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
            return "retry"
        if self.notify_on_done:
            notify(
                "folder-watch copied",
                f"{path.name} ({human_size(size)}) → {self.dest.name}",
                reveal=dest,
            )
        return "copied"

