"""Atomic copy with retries."""
from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
from pathlib import Path

from folder_watch.support.log import emit

# A copy killed mid-flight leaves its temp behind. Nothing swept them up, so a
# destination accumulated full-size hidden files that neither a restart nor
# `prune` touched. Old enough that a copy genuinely in flight is never hit.
STALE_TMP_AGE = 3600.0


def sweep_stale_temps(dest: Path, *, max_age: float = STALE_TMP_AGE) -> int:
    """Remove leftovers from earlier interrupted copies of this destination."""
    removed = 0
    now = time.time()
    try:
        candidates = list(dest.parent.glob(f".{dest.name}.*.tmp"))
    except OSError:
        return 0
    for leftover in candidates:
        try:
            if now - leftover.stat().st_mtime < max_age:
                continue
            leftover.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    if removed:
        emit("cleanup", reason="stale_temp", count=removed, dest=dest.parent)
    return removed


def atomic_copy(
    src: Path,
    dest: Path,
    *,
    retries: int = 5,
    backoff: float = 0.5,
) -> None:
    """Copy to dest via temp file + rename; retry transient OSError (Drive).

    The caller owns creating dest.parent — see paths.ensure_dest_dir. Creating
    it here would silently rebuild an unmounted volume's tree on local disk.
    """
    sweep_stale_temps(dest)
    last: OSError | None = None
    for attempt in range(1, retries + 1):
        tmp: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{dest.name}.",
                suffix=".tmp",
                dir=str(dest.parent),
            )
            os.close(fd)
            tmp = Path(tmp_name)
            shutil.copy2(src, tmp)
            try:
                with open(tmp, "rb") as f:
                    os.fsync(f.fileno())
            except OSError:
                pass
            os.replace(tmp, dest)
            tmp = None
            return
        except OSError as e:
            last = e
            if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                # A read-only or forbidden destination will not become writable
                # by waiting; retrying just burns seconds per file.
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            emit(
                # Not the retry queue — this is atomic_copy's own inner backoff.
                "copy_retry",
                name=src.name,
                attempt=attempt,
                err=str(e),
                sleep=backoff * attempt,
            )
            time.sleep(backoff * attempt)
    assert last is not None
    raise last
