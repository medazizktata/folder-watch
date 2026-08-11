"""ConvX watch wrapper."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def build_convx_watch_cmd(
    src: Path,
    *,
    format: str | None = None,
    convx_preset: str | None = None,
    quality: int | None = None,
    width: int | None = None,
    fps: float | None = None,
    max_size: int | None = None,
    overwrite: bool = True,
    filter_exts: list[str] | None = None,
    debounce_ms: int = 500,
    convx_bin: str | None = None,
) -> list[str]:
    """Map folder-watch flags → `convx watch` argv. ConvX owns FSEvents + convert."""
    if not format and not convx_preset:
        raise ValueError("convx requires --format and/or --convx-preset")

    bin_path = convx_bin or shutil.which("convx") or "convx"
    cmd: list[str] = [bin_path, "watch", str(src.expanduser().resolve())]

    if format:
        cmd.extend(["-t", format.lower().lstrip(".")])
    if convx_preset:
        cmd.extend(["-p", convx_preset])
    if filter_exts:
        filt = ",".join(
            e if e.startswith("*.") else f"*.{e.lstrip('.')}" for e in filter_exts
        )
        cmd.extend(["--filter", filt])
    if debounce_ms is not None:
        cmd.extend(["--debounce", str(int(debounce_ms))])
    if quality is not None:
        cmd.extend(["-q", str(quality)])
    if width is not None:
        cmd.extend(["-w", str(width)])
    if fps is not None:
        cmd.extend(["--fps", str(fps)])
    if max_size is not None:
        cmd.extend(["--max-size", str(max_size)])
    if overwrite:
        cmd.append("--overwrite")
    return cmd


def run_convx_watch(cmd: list[str]) -> int:
    print(f"folder-watch → {' '.join(cmd)}", flush=True)
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print(f"error: convx not found ({cmd[0]!r})", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0
