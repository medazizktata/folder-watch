"""Support helpers: logging, persistent done-state, atomic copy, LaunchAgent."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".local" / "state" / "folder-watch"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.folder-watch"

log = logging.getLogger("folder_watch")


def setup_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    json_mode: bool = False,
) -> None:
    """Configure folder_watch logger. Events go to stdout via emit(); optional file."""
    lvl = getattr(logging, level.upper(), logging.INFO)
    log.handlers.clear()
    log.setLevel(lvl)
    log.propagate = False

    fmt = (
        logging.Formatter("%(message)s")
        if json_mode
        else logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    )
    # Keep stderr quiet for info events (those print via emit → stdout)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    log.addHandler(err)

    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(lvl)
        log.addHandler(fh)


_JSON_MODE = False


def set_json_mode(on: bool) -> None:
    global _JSON_MODE
    _JSON_MODE = on


def emit(event: str, **fields: Any) -> None:
    """Human key=value line or JSON object; also to logger."""
    payload = {"event": event, **{k: _jsonable(v) for k, v in fields.items()}}
    if _JSON_MODE:
        line = json.dumps(payload, ensure_ascii=False)
    else:
        bits = [event] + [f"{k}={_fmt(v)}" for k, v in fields.items()]
        line = " ".join(bits)
    # stdout for pipe-friendly watch output (keeps prior UX)
    print(line, flush=True)
    log.info("%s", line)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    return v


def _fmt(v: Any) -> str:
    if isinstance(v, Path):
        return str(v)
    return str(v)


# ---------------------------------------------------------------------------
# Done-state (scheme B: action + src + dest/mode + size + mtime)
# ---------------------------------------------------------------------------


class DoneStore:
    """Persistent done records so restarts don't re-copy the same work."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def path_for(action: str, src: Path, *, to: str | None = None, mode: str | None = None) -> Path:
        raw = f"{action}|{src.expanduser().resolve()}|{to or ''}|{mode or ''}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return STATE_DIR / f"done-{action}-{digest}.json"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            self._data = data["entries"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": self._data}
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix="done-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def make_key(
        action: str,
        src: Path,
        *,
        size: int,
        mtime_ns: int,
        dest: Path | None = None,
        mode: str | None = None,
    ) -> str:
        dest_s = str(dest.expanduser().resolve()) if dest else ""
        src_s = str(src.expanduser().resolve())
        return f"{action}|{src_s}|{dest_s}|{mode or ''}|{size}|{mtime_ns}"

    def is_done(self, key: str) -> bool:
        return key in self._data

    def mark(self, key: str, **meta: Any) -> None:
        self._data[key] = {"ts": time.time(), **{k: _jsonable(v) for k, v in meta.items()}}
        self.save()

    def mark_baseline_file(
        self,
        action: str,
        src: Path,
        *,
        size: int,
        mtime_ns: int,
        dest: Path | None = None,
        mode: str | None = None,
    ) -> str:
        key = self.make_key(
            action, src, size=size, mtime_ns=mtime_ns, dest=dest, mode=mode
        )
        if not self.is_done(key):
            self.mark(key, baseline=True, name=src.name)
        return key


def file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))


# ---------------------------------------------------------------------------
# Atomic copy + retry
# ---------------------------------------------------------------------------


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
            # best-effort fsync
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


# ---------------------------------------------------------------------------
# LaunchAgent
# ---------------------------------------------------------------------------


def service_label(preset: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in preset)
    return f"{LABEL_PREFIX}.{safe}"


def service_plist_path(preset: str) -> Path:
    return LAUNCH_AGENTS / f"{service_label(preset)}.plist"


def install_service(
    *,
    preset: str,
    program: Path,
    log_path: Path | None = None,
) -> Path:
    """Write LaunchAgent plist and load it."""
    label = service_label(preset)
    plist = service_plist_path(preset)
    log_path = (log_path or (STATE_DIR / f"{preset}.log")).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    program = program.expanduser().resolve()
    # Prefer project bin wrapper if present
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{program}</string>
    <string>--preset</string>
    <string>{preset}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
</dict>
</plist>
"""
    plist.write_text(body)
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
    r = subprocess.run(
        ["launchctl", "load", str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "launchctl load failed")
    return plist


def uninstall_service(*, preset: str) -> Path | None:
    plist = service_plist_path(preset)
    if plist.is_file():
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
        plist.unlink(missing_ok=True)
        return plist
    return None


def service_status(*, preset: str) -> dict[str, Any]:
    label = service_label(preset)
    plist = service_plist_path(preset)
    r = subprocess.run(
        ["launchctl", "list", label],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "label": label,
        "plist": str(plist),
        "plist_exists": plist.is_file(),
        "loaded": r.returncode == 0,
        "launchctl": (r.stdout or r.stderr or "").strip(),
    }
