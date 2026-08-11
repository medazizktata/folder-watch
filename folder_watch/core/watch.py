"""Watch loops: settle, poll, event."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from folder_watch.actions.base import Action
from folder_watch.core.util import is_open, should_watch_name
from folder_watch.support.log import emit, emit_debug, is_verbose
from folder_watch.support.pidfile import clear_pidfile, write_pidfile
from folder_watch.support.state import DoneStore, file_identity

def _state_key(src: Path) -> str:
    """Full path key so nested same-basename files don't collide."""
    try:
        return str(src.resolve())
    except OSError:
        return str(src)


def iter_watch_files(
    src_dir: Path,
    exts: list[str] | None,
    *,
    recursive: bool = False,
) -> list[Path]:
    """Files under src_dir matching exts (optionally nested)."""
    out: list[Path] = []
    try:
        if not recursive:
            for n in os.listdir(src_dir):
                if not should_watch_name(n, exts):
                    continue
                p = src_dir / n
                if p.is_file():
                    out.append(p)
            return out
        for root, _dirs, files in os.walk(src_dir):
            for n in files:
                if not should_watch_name(n, exts):
                    continue
                p = Path(root) / n
                if p.is_file():
                    out.append(p)
    except FileNotFoundError:
        emit("warn", reason="src_missing", src=src_dir)
    return out


def apply_action(
    src: Path,
    action: Action,
    *,
    state: dict[str, dict],
    size: int,
    stable_needed: int,
    done_store: DoneStore | None = None,
) -> None:
    name = src.name
    sk = _state_key(src)
    try:
        size_now, mtime_ns = file_identity(src)
    except OSError:
        return
    size = size_now

    prev_state = state.get(sk, {})
    if prev_state.get("done") and prev_state.get("size") == size:
        return

    dest = action.done_dest(src)
    mode = action.done_mode()
    key = None
    if done_store is not None:
        key = DoneStore.make_key(
            action.name,
            src,
            size=size,
            mtime_ns=mtime_ns,
            dest=dest,
            mode=mode,
        )
        if done_store.is_done(key):
            state[sk] = {"size": size, "stable": stable_needed, "done": True}
            emit("skip", name=name, size=size, reason="done_store")
            return

    try:
        result = action.run(src)
    except Exception as e:  # noqa: BLE001
        emit("error", name=name, err=str(e))
        return

    if result == "skip":
        emit("skip", name=name, size=size)
    elif result == "copied":
        emit("copied", name=name, size=size)
    elif result in ("would_copy", "would_extract"):
        # dry-run: already emitted inside action; do not persist done
        return
    elif result in ("extracted", "converted"):
        pass
    elif result == "fail":
        emit("fail", name=name)
        return
    else:
        emit("result", name=name, status=result)
        if result == "error":
            return

    state[sk] = {"size": size, "stable": stable_needed, "done": True}
    if done_store is not None and key is not None:
        done_store.mark(key, name=name, result=result)


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
        interval = 0.0 if is_verbose() else 2.0
        if now - track.get("last_log", 0) >= interval:
            emit("progress", name=src.name, size=size, open=open_n)
            track["last_log"] = now
        return "waiting"

    prev = track.get("prev")
    if prev is None or size != prev:
        track["prev"] = size
        track["stable"] = 1
        emit_debug("settle", name=src.name, size=size, stable=1, reason="size_change")
    else:
        track["stable"] = int(track.get("stable", 0)) + 1
        emit_debug(
            "settle",
            name=src.name,
            size=size,
            stable=track["stable"],
            need=stable_needed,
        )

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
    done_store: DoneStore | None = None,
) -> None:
    """Settle then action.

    wait=True  — block until ready (legacy / rare)
    wait=False — never block on in-progress files; skip and let event loop retry
    """
    track: dict[str, Any] = {}

    if not wait:
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
                    done_store=done_store,
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
                done_store=done_store,
            )
            return
        time.sleep(settle_interval)


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
    done_store: DoneStore | None = None,
    recursive: bool = False,
) -> None:
    paths = iter_watch_files(src_dir, exts, recursive=recursive)
    if not paths and not src_dir.is_dir():
        return

    for path in paths:
        process_file(
            path,
            action,
            state=state,
            min_bytes=min_bytes,
            stable_needed=stable_needed,
            settle_interval=settle_interval,
            wait=wait,
            done_store=done_store,
        )


def seed_baseline(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    min_bytes: int,
    state: dict[str, dict],
    done_store: DoneStore | None = None,
    recursive: bool = False,
) -> list[Path]:
    """Ignore finished files already in SRC. Return paths still being written."""
    unsettled: list[Path] = []
    ignored = 0
    paths = iter_watch_files(src_dir, exts, recursive=recursive)

    for path in paths:
        try:
            size, mtime_ns = file_identity(path)
            resolved = path.resolve()
        except OSError:
            continue
        if is_open(path) or size < min_bytes:
            unsettled.append(resolved)
            continue
        state[_state_key(path)] = {"size": size, "done": True, "baseline": True}
        if done_store is not None:
            done_store.mark_baseline_file(
                action.name,
                path,
                size=size,
                mtime_ns=mtime_ns,
                dest=action.done_dest(path),
                mode=action.done_mode(),
            )
        ignored += 1

    emit(
        "baseline",
        ignored=ignored,
        encoding=len(unsettled),
        note="old finished skipped; only new + active encodes",
        recursive=recursive,
    )
    return unsettled


def run_watch_poll(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    poll: float,
    stable_needed: int,
    min_bytes: int,
    catch_up: bool = False,
    done_store: DoneStore | None = None,
    recursive: bool = False,
) -> int:
    state: dict[str, dict] = {}
    if not catch_up:
        seed_baseline(
            src_dir,
            action,
            exts=exts,
            min_bytes=min_bytes,
            state=state,
            done_store=done_store,
            recursive=recursive,
        )
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
            done_store=done_store,
            recursive=recursive,
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
    done_store: DoneStore | None = None,
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
            done_store=done_store,
            recursive=recursive,
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
            if not should_watch_name(path.name, exts):
                return
            # Ignore baseline files unless size changed (re-export)
            st = state.get(_state_key(path))
            if st and st.get("done") and st.get("baseline"):
                try:
                    if path.stat().st_size == st.get("size"):
                        return
                except OSError:
                    return
            key = str(path)
            try:
                rel = str(path.relative_to(src_dir))
            except ValueError:
                rel = path.name
            with lock:
                pending[key] = time.monotonic()
                watching.pop(key, None)
                emit("event", name=path.name, path=rel)

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
            done_store=done_store,
            recursive=recursive,
        )
        for p in iter_watch_files(src_dir, exts, recursive=recursive):
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if state.get(_state_key(resolved), {}).get("done"):
                continue
            with lock:
                pending[str(resolved)] = time.monotonic()
    else:
        unsettled = seed_baseline(
            src_dir,
            action,
            exts=exts,
            min_bytes=min_bytes,
            state=state,
            done_store=done_store,
            recursive=recursive,
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
                    done_store=done_store,
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
    done_store: DoneStore | None = None,
    watch_name: str | None = None,
    log_path: Path | None = None,
    preset: str | None = None,
    to: str | None = None,
) -> int:
    src_dir = src_dir.expanduser().resolve()
    name = watch_name or preset or f"{action.name}-{src_dir.name}"

    emit(
        "start",
        src=src_dir,
        action=action.describe(),
        ext=exts or "*",
        backend=backend,
        debounce_ms=debounce_ms,
        stable=stable_needed,
        settle=settle_interval,
        min_bytes=min_bytes,
        recursive=recursive,
        catch_up=(catch_up if not once else "n/a"),
        mode=("once" if once else "watch"),
        done_store=(str(done_store.path) if done_store else None),
    )

    if not once:
        write_pidfile(
            name,
            {
                "name": name,
                "preset": preset,
                "src": str(src_dir),
                "action": action.name,
                "to": to,
                "log": str(log_path) if log_path else None,
            },
        )

    try:
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
                done_store=done_store,
                recursive=recursive,
            )
            emit("done")
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
                done_store=done_store,
                recursive=recursive,
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
            done_store=done_store,
        )
    finally:
        if not once:
            clear_pidfile(name)
