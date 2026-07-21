#!/usr/bin/env python3
"""folder-watch — watch a folder; run a pluggable action when files finish writing.

Core (default): FSEvents/inotify via watchdog + debounce + lsof/size settle.
Fallback: --backend poll

Built-in actions:
  copy   — copy finished files to another folder (never deletes source)
  unzip  — extract .zip then delete the archive only after verified success
  convx  — thin wrapper around `convx watch` (ConvX owns watching + convert)

Examples:
  folder-watch copy ./Output --to ./Drive/V1 --ext mp4
  folder-watch unzip ~/Downloads/inbox
  folder-watch convx ~/Inbox --format webp --ext png,jpg
  folder-watch --preset la-chaise-v1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "folder-watch"
PRESETS_PATH = CONFIG_DIR / "presets.json"
LEGACY_PRESETS = Path.home() / ".config" / "watch-copy" / "presets.json"

DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "la-chaise-v1": {
        "action": "copy",
        "src": "/Users/MAC/Desktop/project-batch2/Output",
        "to": (
            "/Users/MAC/Library/CloudStorage/"
            "GoogleDrive-you@example.com/"
            ".shortcut-targets-by-id/SHORTCUT_ID/"
            "Client/Project/Batch 2/Output/Videos /V1"
        ),
        "ext": ["mp4"],
        "poll": 30,
        "stable": 3,
        "min_bytes": 1_000_000,
        "backend": "event",
        "debounce_ms": 500,
    }
}


def is_open(path: Path) -> int:
    """Count writers on path (AME encode). Ignore read-only opens (preview)."""
    try:
        r = subprocess.run(
            ["lsof", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    n = 0
    for ln in r.stdout.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 4:
            continue
        fd = parts[3]
        # lsof FD mode suffix: u=read/write, w=write, r=read-only
        if fd and fd[-1] in ("u", "w"):
            n += 1
    return n


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024**2:
        return f"{n / 1024:.1f}KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f}MB"
    return f"{n / 1024**3:.1f}GB"


def reveal_in_finder(path: Path) -> None:
    """Open Finder and select/highlight the file (or its parent if missing)."""
    target = path.expanduser()
    try:
        target = target.resolve()
    except OSError:
        pass
    if not target.exists():
        target = target.parent
    # open -R = reveal & select in Finder
    try:
        subprocess.run(["open", "-R", str(target)], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return
    # bring Finder to front
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to activate'],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def notify(
    title: str,
    body: str,
    *,
    reveal: Path | None = None,
) -> None:
    """macOS notification. If reveal is set, offers 'Show in folder' (alerter)."""

    def _run() -> None:
        reveal_path = reveal.expanduser() if reveal is not None else None
        alerter = shutil.which("alerter")
        if alerter and reveal_path is not None:
            try:
                r = subprocess.run(
                    [
                        alerter,
                        "--title",
                        title,
                        "--message",
                        body,
                        "--subtitle",
                        "Show in folder highlights the file",
                        "--actions",
                        "Show in folder",
                        "--close-label",
                        "Close",
                        "--timeout",
                        "45",
                        "--group",
                        "folder-watch-copy",
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
            except (OSError, subprocess.TimeoutExpired):
                return
            raw = (r.stdout or "").strip()
            atype = ""
            avalue = ""
            try:
                payload = json.loads(raw) if raw.startswith("{") else {}
                atype = str(payload.get("activationType") or "")
                avalue = str(payload.get("activationValue") or "")
            except json.JSONDecodeError:
                avalue = raw

            # Button click, or click on the notification body
            want_reveal = (
                avalue == "Show in folder"
                or atype in ("actionClicked", "contentsClicked")
                or (
                    "show" in avalue.lower()
                    and "folder" in avalue.lower()
                )
            )
            if want_reveal:
                reveal_in_finder(reveal_path)
            return

        # Fallback: click notification to reveal (terminal-notifier)
        tn = shutil.which("terminal-notifier")
        if tn and reveal_path is not None:
            # Build a tiny reveal script so quoting survives Drive paths
            script = (
                "#!/bin/bash\n"
                f"open -R {json.dumps(str(reveal_path))}\n"
                "osascript -e 'tell application \"Finder\" to activate'\n"
            )
            try:
                import tempfile as _tf

                with _tf.NamedTemporaryFile(
                    "w", suffix="-fw-reveal.sh", delete=False
                ) as fh:
                    fh.write(script)
                    sh = fh.name
                os.chmod(sh, 0o755)
                subprocess.run(
                    [
                        tn,
                        "-title",
                        title,
                        "-message",
                        body,
                        "-subtitle",
                        "Click to show file in Finder",
                        "-group",
                        "folder-watch-copy",
                        "-execute",
                        sh,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            return

        # Last resort: plain osascript (no button)
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{esc(body)}" with title "{esc(title)}"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    threading.Thread(target=_run, daemon=True, name="fw-notify").start()


def match_ext(name: str, exts: list[str] | None) -> bool:
    if not exts:
        return True
    lower = name.lower()
    return any(lower.endswith("." + e.lower().lstrip(".")) for e in exts)


def parse_exts(raw: str) -> list[str] | None:
    if raw.strip() == "*":
        return None
    return [e.strip() for e in raw.split(",") if e.strip()]


def load_presets() -> dict[str, dict[str, Any]]:
    merged = dict(DEFAULT_PRESETS)
    for path in (LEGACY_PRESETS, PRESETS_PATH):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            norm = dict(cfg)
            if "dest" in norm and "to" not in norm:
                norm["to"] = norm.pop("dest")
            if "action" not in norm:
                norm["action"] = "copy"
            merged[name] = {**merged.get(name, {}), **norm}
    return merged


def save_preset(name: str, cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    presets: dict[str, Any] = {}
    if PRESETS_PATH.is_file():
        try:
            presets = json.loads(PRESETS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            presets = {}
    presets[name] = cfg
    PRESETS_PATH.write_text(json.dumps(presets, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class Action(ABC):
    name: str
    default_ext: str = "*"
    default_min_bytes: int = 1

    @abstractmethod
    def run(self, path: Path) -> str:
        """Return action result label."""

    def describe(self) -> str:
        return self.name


class CopyAction(Action):
    name = "copy"
    default_ext = "mp4"
    default_min_bytes = 1_000_000

    def __init__(self, dest: Path, *, notify_on_done: bool = True) -> None:
        self.dest = dest.expanduser()
        self.notify_on_done = notify_on_done

    def describe(self) -> str:
        n = " notify" if self.notify_on_done else ""
        return f"copy -> {self.dest}{n}"

    def run(self, path: Path) -> str:
        dest = self.dest / path.name
        size = path.stat().st_size
        if dest.is_file() and dest.stat().st_size == size:
            return "skip"
        self.dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        if dest.stat().st_size != size:
            if self.notify_on_done:
                notify("folder-watch copy failed", path.name, reveal=dest)
            return "fail"
        if self.notify_on_done:
            notify(
                "folder-watch copied",
                f"{path.name} ({human_size(size)}) → {self.dest.name}",
                reveal=dest,
            )
        return "copied"


class UnzipAction(Action):
    name = "unzip"
    default_ext = "zip"
    default_min_bytes = 1

    def __init__(self, mode: str = "named", to: Path | None = None) -> None:
        if mode not in ("named", "same", "to"):
            raise ValueError(f"unknown unzip mode: {mode}")
        if mode == "to" and to is None:
            raise ValueError("--mode to requires --to DIR")
        self.mode = mode
        self.to = to.expanduser() if to else None

    def describe(self) -> str:
        if self.mode == "to":
            return f"unzip mode=to -> {self.to}/<stem>/"
        return f"unzip mode={self.mode}"

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
            print(f"fail name={path.name} reason=not_a_zip", flush=True)
            return "fail"

        target = self._target_dir(path)
        target.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    member = Path(info.filename)
                    if member.is_absolute() or ".." in member.parts:
                        print(
                            f"fail name={path.name} reason=unsafe_member {info.filename}",
                            flush=True,
                        )
                        return "fail"
                members = zf.namelist()
                zf.extractall(target)
        except (zipfile.BadZipFile, OSError) as e:
            print(f"fail name={path.name} reason=extract_error err={e}", flush=True)
            return "fail"

        ok = True
        if members:
            found = any((target / name).exists() for name in members)
            ok = found
        if not ok:
            print(
                f"fail name={path.name} reason=verify_missing_files dest={target}",
                flush=True,
            )
            return "fail"

        try:
            path.unlink()
        except OSError as e:
            print(f"error name={path.name} reason=delete_failed err={e}", flush=True)
            return "error"

        print(f"extracted name={path.name} dest={target} deleted_zip=yes", flush=True)
        return "extracted"


ACTIONS = {
    "copy": CopyAction,
    "unzip": UnzipAction,
}
ACTION_NAMES = ("copy", "unzip", "convx")


# ---------------------------------------------------------------------------
# ConvX — pure wrapper around `convx watch`
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------


def apply_action(
    src: Path,
    action: Action,
    *,
    state: dict[str, dict],
    size: int,
    stable_needed: int,
) -> None:
    name = src.name
    prev_state = state.get(name, {})
    if prev_state.get("done") and prev_state.get("size") == size:
        return

    try:
        result = action.run(src)
    except Exception as e:  # noqa: BLE001
        print(f"error name={name} err={e}", flush=True)
        return

    if result == "skip":
        print(f"skip name={name} size={size}", flush=True)
    elif result == "copied":
        print(f"copied name={name} size={size}", flush=True)
    elif result in ("extracted", "converted"):
        pass
    elif result == "fail":
        print(f"fail name={name}", flush=True)
        return
    else:
        print(f"result name={name} status={result}", flush=True)
        if result == "error":
            return

    state[name] = {"size": size, "stable": stable_needed, "done": True}


def settle_step(
    src: Path,
    track: dict[str, Any],
    *,
    min_bytes: int,
    stable_needed: int,
) -> str:
    """One non-blocking settle check.

    Returns:
      ready   — stable enough to run action (track['size'] set)
      waiting — still open / growing / not enough stable ticks
      gone    — file disappeared
    """
    try:
        size = src.stat().st_size
    except OSError:
        return "gone"

    open_n = is_open(src)
    if open_n or size < min_bytes:
        track["stable"] = 0
        track["prev"] = size
        now = time.monotonic()
        if now - track.get("last_log", 0) >= 2.0:
            print(f"progress name={src.name} size={size} open={open_n}", flush=True)
            track["last_log"] = now
        return "waiting"

    prev = track.get("prev")
    if prev is None or size != prev:
        track["prev"] = size
        track["stable"] = 1
    else:
        track["stable"] = int(track.get("stable", 0)) + 1

    if track["stable"] >= stable_needed:
        track["size"] = size
        return "ready"
    return "waiting"


def process_file(
    src: Path,
    action: Action,
    *,
    state: dict[str, dict],
    min_bytes: int,
    stable_needed: int,
    settle_interval: float,
    wait: bool = True,
) -> None:
    """Settle then action.

    wait=True  — block until ready (legacy / rare)
    wait=False — never block on in-progress files; skip and let event loop retry
    """
    track: dict[str, Any] = {}

    if not wait:
        # Quick path: bail immediately if still encoding; otherwise collect stable ticks.
        for _ in range(stable_needed + 5):
            status = settle_step(
                src, track, min_bytes=min_bytes, stable_needed=stable_needed
            )
            if status == "ready":
                apply_action(
                    src,
                    action,
                    state=state,
                    size=int(track["size"]),
                    stable_needed=stable_needed,
                )
                return
            if status == "gone":
                return
            try:
                if is_open(src) or src.stat().st_size < min_bytes:
                    return
            except OSError:
                return
            time.sleep(settle_interval)
        return

    while True:
        status = settle_step(
            src, track, min_bytes=min_bytes, stable_needed=stable_needed
        )
        if status == "gone":
            return
        if status == "ready":
            apply_action(
                src,
                action,
                state=state,
                size=int(track["size"]),
                stable_needed=stable_needed,
            )
            return
        time.sleep(settle_interval)


def seed_baseline(
    src_dir: Path,
    *,
    exts: list[str] | None,
    min_bytes: int,
    state: dict[str, dict],
) -> list[Path]:
    """Ignore finished files already in SRC. Return paths still being written."""
    unsettled: list[Path] = []
    ignored = 0
    try:
        names = [
            n
            for n in os.listdir(src_dir)
            if match_ext(n, exts) and (src_dir / n).is_file()
        ]
    except FileNotFoundError:
        print(f"warn: src missing: {src_dir}", flush=True)
        return unsettled

    for name in names:
        path = src_dir / name
        try:
            size = path.stat().st_size
            resolved = path.resolve()
        except OSError:
            continue
        # Actively encoding (write handle) or not a finished export yet
        if is_open(path) or size < min_bytes:
            unsettled.append(resolved)
            continue
        state[name] = {"size": size, "done": True, "baseline": True}
        ignored += 1

    print(
        f"baseline ignored={ignored} encoding={len(unsettled)} "
        f"(old finished skipped; only new + active encodes)",
        flush=True,
    )
    return unsettled


def scan_once(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    min_bytes: int,
    stable_needed: int,
    settle_interval: float,
    state: dict[str, dict],
    wait: bool = False,
) -> None:
    try:
        names = [
            n
            for n in os.listdir(src_dir)
            if match_ext(n, exts) and (src_dir / n).is_file()
        ]
    except FileNotFoundError:
        print(f"warn: src missing: {src_dir}", flush=True)
        return

    for name in names:
        process_file(
            src_dir / name,
            action,
            state=state,
            min_bytes=min_bytes,
            stable_needed=stable_needed,
            settle_interval=settle_interval,
            wait=wait,
        )


def run_watch_poll(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    poll: float,
    stable_needed: int,
    min_bytes: int,
    catch_up: bool = False,
) -> int:
    state: dict[str, dict] = {}
    if not catch_up:
        seed_baseline(src_dir, exts=exts, min_bytes=min_bytes, state=state)
    while True:
        scan_once(
            src_dir,
            action,
            exts=exts,
            min_bytes=min_bytes,
            stable_needed=stable_needed,
            settle_interval=min(poll, 1.0),
            state=state,
            wait=False,
        )
        time.sleep(poll)


def run_watch_event(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    debounce_ms: int,
    stable_needed: int,
    min_bytes: int,
    settle_interval: float,
    recursive: bool,
    catch_up: bool = False,
) -> int:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print(
            "error: watchdog not installed — use project .venv "
            "(pip install watchdog). Falling back to --backend poll.",
            file=sys.stderr,
            flush=True,
        )
        return run_watch_poll(
            src_dir,
            action,
            exts=exts,
            poll=max(settle_interval, 1.0),
            stable_needed=stable_needed,
            min_bytes=min_bytes,
            catch_up=catch_up,
        )

    state: dict[str, dict] = {}
    pending: dict[str, float] = {}
    watching: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    class Handler(FileSystemEventHandler):
        def on_created(self, event):  # type: ignore[no-untyped-def]
            self._note(event)

        def on_modified(self, event):  # type: ignore[no-untyped-def]
            self._note(event)

        def on_moved(self, event):  # type: ignore[no-untyped-def]
            if getattr(event, "dest_path", None):

                class _E:
                    is_directory = event.is_directory
                    src_path = event.dest_path

                self._note(_E())

        def _note(self, event):  # type: ignore[no-untyped-def]
            if event.is_directory:
                return
            path = Path(event.src_path)
            try:
                path = path.resolve()
            except OSError:
                return
            if not recursive and path.parent != src_dir:
                return
            if recursive and src_dir not in path.parents and path.parent != src_dir:
                return
            if not match_ext(path.name, exts):
                return
            # Ignore baseline files unless size changed (re-export)
            st = state.get(path.name)
            if st and st.get("done") and st.get("baseline"):
                try:
                    if path.stat().st_size == st.get("size"):
                        return
                except OSError:
                    return
            key = str(path)
            with lock:
                pending[key] = time.monotonic()
                watching.pop(key, None)
                print(f"event name={path.name}", flush=True)

    handler = Handler()
    observer = Observer()
    observer.schedule(handler, str(src_dir), recursive=recursive)
    observer.start()

    if catch_up:
        scan_once(
            src_dir,
            action,
            exts=exts,
            min_bytes=min_bytes,
            stable_needed=stable_needed,
            settle_interval=settle_interval,
            state=state,
            wait=False,
        )
        try:
            for n in os.listdir(src_dir):
                if not match_ext(n, exts):
                    continue
                p = (src_dir / n).resolve()
                if not p.is_file() or state.get(n, {}).get("done"):
                    continue
                with lock:
                    pending[str(p)] = time.monotonic()
        except FileNotFoundError:
            pass
    else:
        unsettled = seed_baseline(
            src_dir, exts=exts, min_bytes=min_bytes, state=state
        )
        now = time.monotonic()
        with lock:
            for p in unsettled:
                pending[str(p)] = now

    debounce_s = max(debounce_ms, 50) / 1000.0

    try:
        while True:
            time.sleep(0.05)
            now = time.monotonic()

            with lock:
                for key, ts in list(pending.items()):
                    if now - ts < debounce_s:
                        continue
                    pending.pop(key, None)
                    if key not in watching:
                        watching[key] = {"next_check": now, "stable": 0}

                due = [
                    key
                    for key, tr in watching.items()
                    if tr.get("next_check", 0) <= now
                ]

            for key in due:
                path = Path(key)
                if not path.is_file():
                    with lock:
                        watching.pop(key, None)
                    continue

                with lock:
                    track = watching.get(key)
                    if track is None:
                        continue

                status = settle_step(
                    path,
                    track,
                    min_bytes=min_bytes,
                    stable_needed=stable_needed,
                )

                if status == "gone":
                    with lock:
                        watching.pop(key, None)
                    continue

                if status == "waiting":
                    with lock:
                        if key in watching:
                            watching[key]["next_check"] = now + settle_interval
                    continue

                with lock:
                    watching.pop(key, None)
                apply_action(
                    path,
                    action,
                    state=state,
                    size=int(track["size"]),
                    stable_needed=stable_needed,
                )
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0
    finally:
        observer.stop()
        observer.join(timeout=5)
    return 0


def run_watch(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    poll: float,
    stable_needed: int,
    min_bytes: int,
    once: bool,
    backend: str = "event",
    debounce_ms: int = 500,
    settle_interval: float = 0.5,
    recursive: bool = False,
    catch_up: bool = False,
) -> int:
    src_dir = src_dir.expanduser().resolve()

    print(
        f"folder-watch src={src_dir} action={action.describe()} "
        f"ext={exts or '*'} backend={backend} "
        f"debounce_ms={debounce_ms} stable={stable_needed} "
        f"settle={settle_interval}s min_bytes={min_bytes} "
        f"catch_up={catch_up if not once else 'n/a'} "
        f"mode={'once' if once else 'watch'}",
        flush=True,
    )

    if once:
        state: dict[str, dict] = {}
        scan_once(
            src_dir,
            action,
            exts=exts,
            min_bytes=min_bytes,
            stable_needed=1,
            settle_interval=0.05,
            state=state,
            wait=False,
        )
        print("done", flush=True)
        return 0

    if backend == "poll":
        return run_watch_poll(
            src_dir,
            action,
            exts=exts,
            poll=poll,
            stable_needed=stable_needed,
            min_bytes=min_bytes,
            catch_up=catch_up,
        )

    return run_watch_event(
        src_dir,
        action,
        exts=exts,
        debounce_ms=debounce_ms,
        stable_needed=stable_needed,
        min_bytes=min_bytes,
        settle_interval=settle_interval,
        recursive=recursive,
        catch_up=catch_up,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="folder-watch",
        description=(
            "Watch a folder for finished files, then run an action "
            "(copy, unzip), or wrap ConvX watch (convx)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "actions:\n"
            "  copy   SRC --to DEST\n"
            "  unzip  SRC [--mode named|same|to] [--to DIR]\n"
            "  convx  SRC --format EXT [--ext png,jpg]   # → convx watch\n"
        ),
    )
    p.add_argument("action", nargs="?", choices=list(ACTION_NAMES), help="Action")
    p.add_argument("src", nargs="?", help="Folder to watch")
    p.add_argument("--to", dest="to", help="Destination / output directory (copy/unzip)")
    p.add_argument(
        "--mode",
        choices=("named", "same", "to"),
        default="named",
        help="unzip: named|same|to",
    )
    p.add_argument("--format", dest="format", help="convx watch -t output format")
    p.add_argument("--convx-preset", dest="convx_preset", help="ConvX -p preset")
    p.add_argument("--quality", type=int, help="convx quality 0-100")
    p.add_argument("--width", type=int, help="convx width")
    p.add_argument("--fps", type=float, help="convx fps")
    p.add_argument("--max-size", type=int, dest="max_size", help="convx max bytes")
    p.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="convx --overwrite (default true)",
    )
    p.add_argument("--convx-bin", default=None, help="Path to convx")
    p.add_argument("--ext", default=None, help="Extensions, or * (→ convx --filter)")
    p.add_argument("--poll", type=float, default=30, help="poll backend interval")
    p.add_argument(
        "--backend",
        choices=("event", "poll"),
        default="event",
        help="copy/unzip: event=FSEvents (default); poll=timer",
    )
    p.add_argument(
        "--debounce",
        type=int,
        default=500,
        dest="debounce_ms",
        help="quiet window ms (default 500); passed to convx watch",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=0.5,
        help="copy/unzip: seconds between size/lsof checks after event",
    )
    p.add_argument("--recursive", action="store_true", help="copy/unzip: watch subdirs")
    p.add_argument(
        "--stable",
        type=int,
        default=3,
        help="copy/unzip: same-size checks before action (default 3)",
    )
    p.add_argument("--min-bytes", type=int, default=None, help="copy/unzip: ignore tiny files")
    p.add_argument("--once", action="store_true", help="copy/unzip: process ready files and exit")
    p.add_argument(
        "--catch-up",
        action="store_true",
        help="also process files already finished in SRC (default: new/in-progress only)",
    )
    p.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="macOS notification on copy success/fail (default: on)",
    )
    p.add_argument("--preset", help="folder-watch JSON preset")
    p.add_argument("--list-presets", action="store_true")
    p.add_argument("--list-actions", action="store_true")
    p.add_argument("--save-preset", metavar="NAME")
    return p


def build_action(name: str, args: argparse.Namespace) -> Action:
    if name == "copy":
        if not args.to:
            raise SystemExit("error: copy requires --to DEST")
        return CopyAction(
            Path(args.to),
            notify_on_done=getattr(args, "notify", True),
        )
    if name == "unzip":
        return UnzipAction(mode=args.mode, to=Path(args.to) if args.to else None)
    raise SystemExit(f"error: unknown action {name!r}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    presets = load_presets()
    passed = {a.split("=")[0] for a in argv}

    if args.list_actions:
        print("copy  — copy finished files to --to DIR (never deletes source)")
        print("unzip — extract .zip; delete only after verified success")
        print("         --mode named (default) | same | to")
        print("convx — wrapper: exec `convx watch` (in-place convert; ConvX owns FSEvents)")
        print("         --format EXT and/or --convx-preset; --ext → --filter")
        print("backend — copy/unzip only: event (default) | poll")
        return 0

    if args.list_presets:
        for name, cfg in presets.items():
            print(f"{name}:")
            for k, v in cfg.items():
                print(f"  {k}: {v}")
        return 0

    action_name = args.action
    src: Path | None = Path(args.src) if args.src else None
    poll = args.poll
    stable = args.stable
    min_bytes = args.min_bytes
    exts_raw = args.ext
    to = args.to
    mode = args.mode
    fmt = args.format
    convx_preset = args.convx_preset
    quality = args.quality
    width = args.width
    fps = args.fps
    max_size = args.max_size
    overwrite = args.overwrite
    convx_bin = args.convx_bin
    backend = args.backend
    debounce_ms = args.debounce_ms
    settle = args.settle
    recursive = args.recursive

    if args.preset:
        if args.preset not in presets:
            print(f"error: unknown preset {args.preset!r}", file=sys.stderr)
            print("known:", ", ".join(presets) or "(none)", file=sys.stderr)
            return 2
        cfg = presets[args.preset]
        action_name = action_name or cfg.get("action", "copy")
        if src is None and cfg.get("src"):
            src = Path(cfg["src"])
        if to is None and cfg.get("to"):
            to = cfg["to"]
            args.to = to
        if "--poll" not in passed:
            poll = float(cfg.get("poll", poll))
        if "--stable" not in passed:
            stable = int(cfg.get("stable", stable))
        if "--backend" not in passed and cfg.get("backend"):
            backend = cfg["backend"]
        if "--debounce" not in passed and cfg.get("debounce_ms") is not None:
            debounce_ms = int(cfg["debounce_ms"])
        if "--settle" not in passed and cfg.get("settle") is not None:
            settle = float(cfg["settle"])
        if "--recursive" not in passed and cfg.get("recursive"):
            recursive = True
        if "--min-bytes" not in passed and cfg.get("min_bytes") is not None:
            min_bytes = int(cfg["min_bytes"])
        if "--ext" not in passed and cfg.get("ext") is not None:
            ext_cfg = cfg["ext"]
            exts_raw = "*" if ext_cfg == [] else ",".join(ext_cfg)
        if "--mode" not in passed and cfg.get("mode"):
            mode = cfg["mode"]
            args.mode = mode
        if "--format" not in passed and cfg.get("format"):
            fmt = cfg["format"]
            args.format = fmt
        if "--convx-preset" not in passed and cfg.get("convx_preset"):
            convx_preset = cfg["convx_preset"]
            args.convx_preset = convx_preset
        if "--quality" not in passed and cfg.get("quality") is not None:
            quality = int(cfg["quality"])
            args.quality = quality
        if "--width" not in passed and cfg.get("width") is not None:
            width = int(cfg["width"])
            args.width = width
        if "--fps" not in passed and cfg.get("fps") is not None:
            fps = float(cfg["fps"])
            args.fps = fps
        if "--max-size" not in passed and cfg.get("max_size") is not None:
            max_size = int(cfg["max_size"])
            args.max_size = max_size
        if (
            "--overwrite" not in passed
            and "--no-overwrite" not in passed
            and "overwrite" in cfg
        ):
            overwrite = bool(cfg["overwrite"])
            args.overwrite = overwrite
        if "--convx-bin" not in passed and cfg.get("convx_bin"):
            convx_bin = cfg["convx_bin"]
            args.convx_bin = convx_bin

    if not action_name or src is None:
        build_parser().print_help()
        return 2

    # convx watch has no output dir; ignore legacy preset "to" unless user passed --to
    if action_name == "convx" and "--to" not in passed:
        to = None

    args.to = to
    args.mode = mode
    args.format = fmt
    args.convx_preset = convx_preset
    args.quality = quality
    args.width = width
    args.fps = fps
    args.max_size = max_size
    args.overwrite = overwrite
    args.convx_bin = convx_bin

    # --- convx: pure wrapper (no folder-watch settle loop) ---
    if action_name == "convx":
        if args.once:
            print(
                "error: convx wraps `convx watch` (continuous). "
                "Use `convx convert` directly for one-shot.",
                file=sys.stderr,
            )
            return 2
        if to:
            print(
                "error: convx watch converts in-place (no --to OUTDIR). "
                "Drop --to, or use `convx convert -d OUTDIR`.",
                file=sys.stderr,
            )
            return 2

        filter_exts = None
        if exts_raw is None:
            exts_raw = "*"
        if exts_raw.strip() != "*":
            filter_exts = parse_exts(exts_raw)

        if args.save_preset:
            cfg: dict[str, Any] = {
                "action": "convx",
                "src": str(src.expanduser()),
                "debounce_ms": debounce_ms,
                "overwrite": overwrite,
                "ext": [] if exts_raw.strip() == "*" else list(filter_exts or []),
            }
            if fmt:
                cfg["format"] = fmt
            if convx_preset:
                cfg["convx_preset"] = convx_preset
            if quality is not None:
                cfg["quality"] = quality
            if width is not None:
                cfg["width"] = width
            if fps is not None:
                cfg["fps"] = fps
            if max_size is not None:
                cfg["max_size"] = max_size
            if convx_bin:
                cfg["convx_bin"] = convx_bin
            save_preset(args.save_preset, cfg)
            print(f"saved preset {args.save_preset!r} -> {PRESETS_PATH}")
            return 0

        try:
            cmd = build_convx_watch_cmd(
                src,
                format=fmt,
                convx_preset=convx_preset,
                quality=quality,
                width=width,
                fps=fps,
                max_size=max_size,
                overwrite=overwrite,
                filter_exts=filter_exts,
                debounce_ms=debounce_ms,
                convx_bin=convx_bin,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return run_convx_watch(cmd)

    action_cls = ACTIONS[action_name]
    if exts_raw is None:
        exts_raw = action_cls.default_ext
    if min_bytes is None:
        min_bytes = action_cls.default_min_bytes

    if args.save_preset:
        cfg = {
            "action": action_name,
            "src": str(src.expanduser()),
            "poll": poll,
            "stable": stable,
            "min_bytes": min_bytes,
            "backend": backend,
            "debounce_ms": debounce_ms,
            "settle": settle,
            "recursive": recursive,
            "ext": [] if exts_raw.strip() == "*" else parse_exts(exts_raw),
        }
        if to:
            cfg["to"] = str(Path(to).expanduser())
        if action_name == "unzip":
            cfg["mode"] = mode
        save_preset(args.save_preset, cfg)
        print(f"saved preset {args.save_preset!r} -> {PRESETS_PATH}")
        return 0

    try:
        action = build_action(action_name, args)
    except (SystemExit, ValueError) as e:
        msg = e.args[0] if getattr(e, "args", None) else e
        print(msg, file=sys.stderr)
        return 2

    exts = parse_exts(exts_raw)

    try:
        return run_watch(
            src,
            action,
            exts=exts,
            poll=poll,
            stable_needed=stable,
            min_bytes=min_bytes,
            once=args.once,
            backend=backend,
            debounce_ms=debounce_ms,
            settle_interval=settle,
            recursive=recursive,
            catch_up=args.catch_up,
        )
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
