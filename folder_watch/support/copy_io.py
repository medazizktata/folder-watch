"""Atomic copy with retries."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from folder_watch.support.log import emit


def atomic_copy(
    src: Path,
    dest: Path,
    *,
    retries: int = 5,
    backoff: float = 0.5,
) -> None:
    """Copy to dest via temp file + rename; retry transient OSError (Drive)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
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
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            emit(
                "retry",
                name=src.name,
                attempt=attempt,
                err=str(e),
                sleep=backoff * attempt,
            )
            time.sleep(backoff * attempt)
    assert last is not None
    raise last
