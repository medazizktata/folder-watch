"""Unzip action."""
from __future__ import annotations

import zipfile
from pathlib import Path

from folder_watch.actions.base import Action
from folder_watch.support.log import emit
from folder_watch.support.paths import DestUnavailable, ensure_dest_dir


class UnzipAction(Action):
    name = "unzip"
    default_ext = "zip"
    default_min_bytes = 1

    def __init__(
        self,
        mode: str = "named",
        to: Path | None = None,
        *,
        dry_run: bool = False,
        create_dest: bool = False,
        no_clobber: bool = False,
    ) -> None:
        if mode not in ("named", "same", "to"):
            raise ValueError(f"unknown unzip mode: {mode}")
        if mode == "to" and to is None:
            raise ValueError("--mode to requires --to DIR")
        self.mode = mode
        self.to = to.expanduser() if to else None
        self.dry_run = dry_run
        self.create_dest = create_dest
        self.no_clobber = no_clobber

    def describe(self) -> str:
        if self.mode == "to":
            base = f"unzip mode=to -> {self.to}/<stem>/"
        else:
            base = f"unzip mode={self.mode}"
        return f"{base} dry-run" if self.dry_run else base

    def destination_ready(self) -> bool:
        if self.mode != "to" or self.to is None:
            return True
        try:
            ensure_dest_dir(self.to, create_parents=self.create_dest)
        except (DestUnavailable, OSError):
            return False
        return True

    def done_mode(self) -> str | None:
        return self.mode

    def done_dest(self, src: Path) -> Path | None:
        return self._target_dir(src)

    def output_exists(self, src: Path) -> bool | None:
        target = self._target_dir(src)
        if self.mode == "same":
            # The target directory is the zip's own folder, so its existence
            # proves nothing. Ask the archive what it should have produced —
            # reading the central directory is cheap, no decompression. Without
            # this, --pending and baseline_pending are structurally blind for
            # mode=same and give a false all-clear.
            try:
                with zipfile.ZipFile(src) as zf:
                    members = [n for n in zf.namelist() if not n.endswith("/")]
            except (zipfile.BadZipFile, OSError):
                return None
            if not members:
                return None
            return all((target / n).exists() for n in members)
        try:
            return target.is_dir() and any(target.iterdir())
        except OSError:
            return None

    def _target_dir(self, zip_path: Path) -> Path:
        stem = zip_path.stem
        if self.mode == "same":
            return zip_path.parent
        if self.mode == "named":
            return zip_path.parent / stem
        assert self.to is not None
        return self.to / stem

    def run(self, path: Path) -> str:
        if path.suffix.lower() != ".zip":
            return "skip"
        if not zipfile.is_zipfile(path):
            emit("fail", name=path.name, reason="not_a_zip")
            return "fail"

        target = self._target_dir(path)
        if self.dry_run:
            emit("would_extract", name=path.name, dest=target, mode=self.mode)
            return "would_extract"

        clobbered: list[str] = []
        try:
            with zipfile.ZipFile(path, "r") as zf:
                # Vet every member before creating anything, so a rejected zip
                # doesn't leave an empty target directory behind.
                for info in zf.infolist():
                    member = Path(info.filename)
                    if member.is_absolute() or ".." in member.parts:
                        emit(
                            "fail",
                            name=path.name,
                            reason="unsafe_member",
                            member=info.filename,
                        )
                        return "fail"
                members = zf.namelist()
                # A member that lands on the archive's own path (a zip
                # containing a file of its own name, under --mode same) would
                # be overwritten by extractall and then deleted as "the
                # archive" — losing the archive AND the extracted content
                # while every check reports success.
                for member in members:
                    try:
                        if (target / member).resolve() == path.resolve():
                            emit(
                                "fail",
                                name=path.name,
                                reason="member_is_archive",
                                member=member,
                            )
                            return "fail"
                    except OSError:
                        continue
                try:
                    # named/same land beside the zip, so their parent exists by
                    # definition. Only --mode to can aim at an unmounted volume,
                    # and it's --to itself that must already be reachable —
                    # the per-zip subfolder under it is ours to create.
                    if self.mode == "to" and self.to is not None:
                        ensure_dest_dir(self.to, create_parents=self.create_dest)
                except DestUnavailable as e:
                    emit("fail", name=path.name, reason="dest_unavailable", err=str(e))
                    return "retry"
                if not members:
                    # Nothing to extract, and deleting the archive would be
                    # the only lasting effect. Usually a truncated download.
                    emit("fail", name=path.name, reason="empty_archive")
                    return "fail"
                clobbered = [n for n in members if (target / n).is_file()]
                if clobbered and self.no_clobber:
                    emit(
                        "fail",
                        name=path.name,
                        reason="would_overwrite",
                        count=len(clobbered),
                        first=clobbered[0],
                    )
                    return "fail"
                target.mkdir(parents=True, exist_ok=True)
                zf.extractall(target)
        except zipfile.BadZipFile as e:
            # Corrupt archives never become valid. Retrying forever is a bug.
            emit("fail", name=path.name, reason="extract_error", err=str(e))
            return "fail"
        except OSError as e:
            # Disk full, volume vanished mid-extract — worth another go later.
            emit("fail", name=path.name, reason="extract_error", err=str(e))
            return "retry"

        # Every member must be present before the archive is destroyed. any()
        # passed when 1 of 200 files landed, and under mode=same it passed on
        # any pre-existing file of the same name.
        missing = [n for n in members if not (target / n).exists()]
        if missing:
            emit(
                "fail",
                name=path.name,
                reason="verify_missing_files",
                dest=target,
                missing=len(missing),
                first=missing[0],
            )
            return "fail"
        if clobbered:
            emit("overwrote", name=path.name, count=len(clobbered), dest=target)

        try:
            path.unlink()
        except OSError as e:
            emit("error", name=path.name, reason="delete_failed", err=str(e))
            return "error"

        emit("extracted", name=path.name, dest=target, deleted_zip=True)
        return "extracted"

