"""Atomic copy with retries."""
from __future__ import annotations

import errno
import hashlib
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


# Read back in chunks; a 40 GB export must not be held in memory to be checked.
DIGEST_CHUNK = 1 << 20


def file_digest(path: Path, *, chunk: int = DIGEST_CHUNK) -> str:
    """sha256 of a file, streamed.

    blake2b would be quicker, but sha256 is what every other tool on the
    machine can reproduce from the command line, and a checksum you cannot
    check by hand independently is worth less than one you can.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def verify_copy(src: Path, dest: Path) -> str | None:
    """Compare src and dest byte for byte, by digest. None means they match.

    Size already agrees by the time this runs — the caller checks that, and it
    is free. This catches the case size cannot: the right number of wrong
    bytes. Rare on local disk, less rare across a network mount or a syncing
    client, and silent when it happens, which is the reason to be able to ask.

    The cost is a full re-read of both files, so it is opt-in.
    """
    try:
        want, got = file_digest(src), file_digest(dest)
    except OSError as e:
        return f"unreadable: {e}"
    return None if want == got else f"sha256 {got[:12]} != {want[:12]}"


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
