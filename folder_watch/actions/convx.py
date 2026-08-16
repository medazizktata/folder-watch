"""ConvX watch wrapper."""
from __future__ import annotations

import shlex
import shutil
import signal
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
    passthru: str | None = None,
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
    if passthru:
        # The escape hatch a wrapper needs to not lag behind the tool it wraps.
        # Anything ConvX grows later is usable the day it ships, without a
        # release here first. Split with shlex and appended to argv — there is
        # no shell in the chain, so this adds arguments, never a command.
        cmd.extend(shlex.split(passthru))
    return cmd


def _stop(proc: subprocess.Popen) -> None:
    """Make sure the child goes when we do.

    `convx watch` runs forever. Anything that ends the wrapper without taking
    the child with it leaves an orphan watching the same folder — and several
    of those quietly stack up over repeated restarts.
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_convx_watch(cmd: list[str]) -> int:
    print(f"folder-watch → {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError:
        print(f"error: convx not found ({cmd[0]!r})", file=sys.stderr)
        return 127

    # Without this SIGTERM kills the wrapper outright, so neither the finally
    # below nor the caller's pidfile cleanup ever runs. launchd happens to
    # reap the whole process group, but nothing else does.
    def _on_term(_signum, _frame):
        raise SystemExit(0)

    try:
        prev_term = signal.signal(signal.SIGTERM, _on_term)
    except ValueError:
        prev_term = None

    try:
        return proc.wait()
    except (KeyboardInterrupt, SystemExit):
        print("\nstopped", flush=True)
        return 0
    finally:
        _stop(proc)
        if prev_term is not None:
            try:
                signal.signal(signal.SIGTERM, prev_term)
            except ValueError:
                pass
