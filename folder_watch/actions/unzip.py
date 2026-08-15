"""Unzip action."""
from __future__ import annotations

import unicodedata
import zipfile
from pathlib import Path

from folder_watch.actions.base import Action
from folder_watch.support.log import emit
from folder_watch.support.paths import (
    DestNotADirectory,
    DestUnavailable,
    ensure_dest_dir,
)


def _fs_collapses_case(directory: Path) -> bool:
    """Does this filesystem treat two differently-cased names as one file?

    APFS and HFS+ do by default; many external and network volumes do not.
    Probed rather than assumed, so a genuinely case-sensitive destination is
    not punished for a check aimed at case-insensitive ones.
    """
    probe = directory / ".folder-watch-case-probe"
    other = directory / ".FOLDER-WATCH-CASE-PROBE"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        return other.exists()
    except OSError:
        return True  # can't tell — assume the risky answer
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def _collision_key(name: str) -> str:
    """Two member names that a case-insensitive filesystem cannot keep apart."""
    return unicodedata.normalize("NFC", name).casefold()


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
        src_root: Path | None = None,
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
        self.src_root = src_root.expanduser() if src_root else None

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
            # Already unique per directory — the target sits beside the zip.
            return zip_path.parent / stem
        assert self.to is not None
        # mode=to collapses the tree without this: under --recursive,
        # clientA/delivery.zip and clientB/delivery.zip both target
        # <to>/delivery/, the second overwrites the first, and BOTH archives
        # are then deleted. copy has src_root for exactly this reason.
        base = self.to
        if self.src_root is not None:
            try:
                rel = zip_path.resolve().parent.relative_to(self.src_root.resolve())
                base = self.to / rel
            except (ValueError, OSError):
                pass
        return base / stem

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
                    as_path = Path(info.filename)
                    if as_path.is_absolute() or ".." in as_path.parts:
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
                for name in members:
                    try:
                        if (target / name).resolve() == path.resolve():
                            emit(
                                "fail",
                                name=path.name,
                                reason="member_is_archive",
                                member=name,
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
                except DestNotADirectory as e:
                    emit(
                        "fail",
                        name=path.name,
                        reason="dest_not_a_directory",
                        err=str(e),
                    )
                    return "fail"
                except DestUnavailable as e:
                    emit("fail", name=path.name, reason="dest_unavailable", err=str(e))
                    return "retry"
                if not [n for n in members if not n.endswith("/")]:
                    # Nothing but directory entries: extracting creates empty
                    # folders and deleting the archive is the only lasting
                    # effect. Usually a truncated download.
                    emit("fail", name=path.name, reason="empty_archive")
                    return "fail"
                # Report.txt / report.txt / REPORT.TXT are three members that
                # a case-insensitive filesystem stores as one file. Every one
                # of them then passes an exists() check, so verification
                # succeeded and the archive was deleted having kept a third of
                # its contents. Probe the filesystem rather than assuming.
                seen: dict[str, str] = {}
                collisions: list[tuple[str, str]] = []
                for name in members:
                    key = _collision_key(name)
                    if key in seen and seen[key] != name:
                        collisions.append((seen[key], name))
                    else:
                        seen.setdefault(key, name)
                if collisions and _fs_collapses_case(target.parent):
                    first, second = collisions[0]
                    emit(
                        "fail",
                        name=path.name,
                        reason="member_case_collision",
                        count=len(collisions),
                        first=first,
                        second=second,
                    )
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

        # Every member must be present, at the size the archive says, before
        # the archive is destroyed. any() passed when 1 of 200 files landed;
        # existence alone still passes when two members collapsed onto one.
        missing: list[str] = []
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    if info.filename.endswith("/"):
                        continue
                    out = target / info.filename
                    try:
                        if not out.is_file() or out.stat().st_size != info.file_size:
                            missing.append(info.filename)
                    except OSError:
                        missing.append(info.filename)
        except (zipfile.BadZipFile, OSError) as e:
            emit("fail", name=path.name, reason="verify_error", err=str(e))
            return "fail"
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

