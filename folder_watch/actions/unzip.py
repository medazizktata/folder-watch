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
    ) -> None:
        if mode not in ("named", "same", "to"):
            raise ValueError(f"unknown unzip mode: {mode}")
        if mode == "to" and to is None:
            raise ValueError("--mode to requires --to DIR")
        self.mode = mode
        self.to = to.expanduser() if to else None
        self.dry_run = dry_run
        self.create_dest = create_dest

    def describe(self) -> str:
        if self.mode == "to":
            base = f"unzip mode=to -> {self.to}/<stem>/"
        else:
            base = f"unzip mode={self.mode}"
        return f"{base} dry-run" if self.dry_run else base

    def done_mode(self) -> str | None:
        return self.mode

    def done_dest(self, src: Path) -> Path | None:
        return self._target_dir(src)

    def output_exists(self, src: Path) -> bool | None:
        # mode=same extracts alongside the zip, so the target directory always
        # exists and proves nothing.
        if self.mode == "same":
            return None
        target = self._target_dir(src)
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
                try:
                    # named/same land beside the zip, so their parent exists by
                    # definition. Only --mode to can aim at an unmounted volume,
                    # and it's --to itself that must already be reachable —
                    # the per-zip subfolder under it is ours to create.
                    if self.mode == "to" and self.to is not None:
                        ensure_dest_dir(self.to, create_parents=self.create_dest)
                except DestUnavailable as e:
                    emit("fail", name=path.name, reason="dest_unavailable", err=str(e))
                    return "fail"
                target.mkdir(parents=True, exist_ok=True)
                zf.extractall(target)
        except (zipfile.BadZipFile, OSError) as e:
            emit("fail", name=path.name, reason="extract_error", err=str(e))
            return "fail"

        ok = True
        if members:
            found = any((target / name).exists() for name in members)
            ok = found
        if not ok:
            emit(
                "fail",
                name=path.name,
                reason="verify_missing_files",
                dest=target,
            )
            return "fail"

        try:
            path.unlink()
        except OSError as e:
            emit("error", name=path.name, reason="delete_failed", err=str(e))
            return "error"

        emit("extracted", name=path.name, dest=target, deleted_zip=True)
        return "extracted"

