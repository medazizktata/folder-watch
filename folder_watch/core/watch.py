"""Watch loops: settle, poll, event."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from folder_watch.actions.base import Action
from folder_watch.core.util import is_open, should_watch_name
from folder_watch.support.log import emit, emit_debug, is_verbose
from folder_watch.support.pidfile import (
    clear_pidfile,
    live_watchers,
    write_pidfile,
)
from folder_watch.support.retry import MAX_PER_SCAN, RetryQueue
from folder_watch.support.state import DoneStore, file_identity

# Drop a watched file after this long with no size change at all. A real
# encode keeps growing, so only genuinely stuck files (aborted exports left
# at 0 bytes, writers that died holding a handle) ever reach it. If the file
# starts moving again a new FSEvent re-queues it.
STALL_TIMEOUT = 300.0

# While a writer still holds a stalled file we must not forget it: releasing a
# handle does not reliably emit an FSEvent, so dropping it abandons finished
# work permanently. Instead back off to a cheap slow re-check, and only truly
# abandon after a full day of no progress.
STALL_RECHECK_INTERVAL = 60.0
STALL_ABANDON_AFTER = 24 * 3600.0

# At startup, only pay for lsof on files touched this recently. An older file
# is treated as finished; if that's wrong, the baseline size check in the
# event handler re-queues it as soon as it grows.
BASELINE_LSOF_WINDOW = 60.0

# How often to confirm the watched directory still exists, and how often to
# re-check while waiting for it to come back. FSEvents goes quiet when the
# directory is deleted or its volume unmounts — without this the process stays
# alive and healthy forever, watching nothing and logging nothing.
SRC_CHECK_INTERVAL = 5.0
SRC_WAIT_INTERVAL = 5.0
SRC_WAIT_LOG_INTERVAL = 60.0


# "retry" counts as a failure for exit-status purposes: in --once there is no
# loop left to retry it, so the caller must be told the work did not land.
FAILED_RESULTS = ("fail", "error", "retry")

# Results whose action has already logged a line with its own detail in it —
# emitting a generic one here would say less and say it twice.
SELF_LOGGED_RESULTS = ("extracted", "converted", "ran")

# Dry-run previews. The action reports them; nothing is persisted as done,
# because nothing was done.
PREVIEW_RESULTS = ("would_copy", "would_extract", "would_exec")

# How often the event loop looks for retries that have come due.
RETRY_SCAN_INTERVAL = 5.0


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
        # The loops own announcing this — emitting here fires every scan.
        emit_debug("warn", reason="src_missing", src=src_dir)
    return out


def apply_action(
    src: Path,
    action: Action,
    *,
    state: dict[str, dict],
    stable_needed: int,
    done_store: DoneStore | None = None,
    lock: threading.Lock | None = None,
    retries: RetryQueue | None = None,
) -> str | None:
    """Run the action on a settled file, returning its result label.

    Callers use the label to set an exit status: --once used to return 0 even
    when every copy failed, so cron and CI could not tell. `lock` guards
    `state` when the caller shares it with a watchdog handler thread."""
    name = src.name
    sk = _state_key(src)
    guard: Any = lock if lock is not None else nullcontext()
    try:
        size, mtime_ns = file_identity(src)
    except OSError:
        return None

    with guard:
        prev_state = state.get(sk, {})
    # Size alone misses a same-length rewrite (a re-exported render, an edit
    # that preserves length), and this path returns without logging anything —
    # so the destination silently keeps the stale copy. mtime is already in
    # hand and is already part of the persistent key; use it here too.
    if (
        prev_state.get("done")
        and prev_state.get("size") == size
        and prev_state.get("mtime_ns") == mtime_ns
    ):
        return None

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
            with guard:
                state[sk] = {
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "stable": stable_needed,
                    "done": True,
                }
            emit("skip", name=name, size=size, reason="done_store")
            return "skip"

    try:
        result = action.run(src)
    except Exception as e:
        emit("error", name=name, err=str(e))
        return "error"

    if result == "skip":
        emit("skip", name=name, size=size)
    elif result == "copied":
        emit("copied", name=name, size=size)
    elif result in PREVIEW_RESULTS:
        # dry-run: already emitted inside action; do not persist done
        return result
    elif result in SELF_LOGGED_RESULTS:
        pass
    elif result == "fail":
        # The action emits its own fail line, with a reason attached.
        if retries is not None:
            retries.drop(src)
        return "fail"
    elif result == "retry":
        # The action judged this transient. Queue it rather than dropping it —
        # a destination coming back produces no FSEvent on the source, so
        # nothing else would ever pick it up again.
        if retries is not None:
            entry = retries.schedule(
                src,
                action=action.name,
                reason="transient",
                size=size,
                mtime_ns=mtime_ns,
            )
            if entry is None:
                emit("warn", name=name, reason="retry_queue_full")
            else:
                emit(
                    "retry_scheduled",
                    name=name,
                    attempt=entry["attempts"],
                    in_s=int(entry["next_due"] - time.time()),
                )
        return "retry"
    else:
        emit("result", name=name, status=result)
        if result == "error":
            return "error"

    with guard:
        state[sk] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "stable": stable_needed,
            "done": True,
        }
    if done_store is not None and key is not None:
        done_store.mark(key, name=name, result=result, src=str(src))
    if retries is not None:
        retries.drop(src)
    return result


def service_retries(
    retries: RetryQueue,
    action: Action,
    *,
    state: dict[str, dict],
    stable_needed: int,
    done_store: DoneStore | None = None,
    lock: threading.Lock | None = None,
    requeue: Callable[[Path], None] | None = None,
    dest_away_logged: bool = False,
) -> bool:
    """Run whatever has come due. Returns the new dest_away_logged flag.

    Shared by both backends. The poll loop went without it entirely, so it
    retried permanent failures on every scan forever, with no backoff and no
    give-up budget.
    """
    for entry in retries.expired():
        emit(
            "give_up",
            name=Path(entry["path"]).name,
            reason="retry_exhausted",
            attempts=entry.get("attempts"),
            hours=int(retries.give_up_after / 3600),
        )
    due = retries.due(limit=MAX_PER_SCAN)
    if due and not action.destination_ready():
        # One probe, not one failure per file.
        retries.defer(due)
        if not dest_away_logged:
            emit("retry_waiting", queued=len(retries), reason="dest_away")
        return True
    if due:
        dest_away_logged = False
    for entry in due:
        rpath = Path(entry["path"])
        if not rpath.is_file():
            retries.drop(rpath)
            continue
        try:
            rst = rpath.stat()
        except OSError:
            continue
        if (rst.st_size, rst.st_mtime_ns) != (entry.get("size"), entry.get("mtime_ns")):
            # Rewritten since it failed — belongs in settle, not here.
            retries.drop(rpath)
            if requeue is not None:
                requeue(rpath)
            continue
        emit("retry", name=rpath.name, attempt=entry.get("attempts"))
        apply_action(
            rpath,
            action,
            state=state,
            stable_needed=stable_needed,
            done_store=done_store,
            lock=lock,
            retries=retries,
        )
    return dest_away_logged


def _progress(track: dict[str, Any], src: Path, size: int) -> None:
    """Throttled progress line — every 2s, or every tick when verbose.

    `open` is reported only when we have a fresh lsof reading; while a file
    is still growing we deliberately don't pay for one.
    """
    now = time.monotonic()
    interval = 0.0 if is_verbose() else 2.0
    if now - track.get("last_log", 0) < interval:
        return
    fields: dict[str, Any] = {"name": src.name, "size": size}
    if "open" in track:
        fields["open"] = track["open"]
    emit("progress", **fields)
    track["last_log"] = now


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

    lsof costs 0.4-0.8s a call (measured), so it is consulted only once the size has held
    still for stable_needed ticks: a file that is still growing is already
    proof of an open write handle, and stat() answers that for ~5ms.
    """
    try:
        size = src.stat().st_size
    except OSError:
        return "gone"

    prev = track.get("prev")
    if prev is None or size != prev:
        # Any movement resets the stall clock the event loop watches.
        track["last_change"] = time.monotonic()

    if size < min_bytes:
        moved = prev is not None and size != prev
        track["stable"] = 0
        track["prev"] = size
        if not track.get("said_small"):
            # Otherwise this only ever emits `progress`, which reads as work in
            # flight, for the full STALL_TIMEOUT before give_up.
            track["said_small"] = True
            emit(
                "ignored",
                name=src.name,
                size=size,
                reason="below_min_bytes",
                min_bytes=min_bytes,
            )
        elif moved:
            # Below the floor but growing towards it — worth a line. Parked at
            # the same size it is not: `progress` reads as work in flight, and
            # the poll backend would repeat it every scan for as long as the
            # file sat there.
            _progress(track, src, size)
        return "waiting"

    changed = prev is None or size != prev
    if changed:
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

    # Falling through on the first observation matters: with --stable 1 one
    # look is the whole requirement, and returning early here cost every
    # --once run an extra settle interval per file.
    if track["stable"] < stable_needed:
        if changed:
            _progress(track, src, size)
        return "waiting"

    # Size has plateaued — now it's worth paying for lsof. If a writer still
    # holds the file, stay above the threshold so the next tick re-checks.
    open_n = is_open(src)
    track["open"] = open_n
    if open_n:
        _progress(track, src, size)
        return "waiting"

    track["size"] = size
    return "ready"


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
    retries: RetryQueue | None = None,
) -> str | None:
    """Settle then action, returning the action's result label (or None).

    wait=True  — block until ready (legacy / rare)
    wait=False — never block on in-progress files; skip and let event loop retry
    """
    sk = _state_key(src)
    # Ask the cheap questions first. The done-store check used to live inside
    # apply_action, downstream of the whole settle pipeline, so a rescan where
    # every file was already done cost the same wall clock and the same lsof
    # calls as doing all the work.
    if done_store is not None:
        try:
            size, mtime_ns = file_identity(src)
        except OSError:
            return None
        prev = state.get(sk, {})
        if (
            prev.get("done")
            and prev.get("size") == size
            and prev.get("mtime_ns") == mtime_ns
        ):
            # Already reported in this process. The poll backend rescans every
            # cycle, so re-emitting here grew the log without bound.
            return prev.get("last_result")
        key = DoneStore.make_key(
            action.name,
            src,
            size=size,
            mtime_ns=mtime_ns,
            dest=action.done_dest(src),
            mode=action.done_mode(),
        )
        if done_store.is_done(key):
            # A baseline entry is an assumption, not a result. If the output
            # isn't there, this file was never processed — saying "skip" and
            # exiting 0 reported total success over untouched work.
            assumed = done_store.is_baseline(key) and action.output_exists(src) is False
            result = "pending" if assumed else "skip"
            emit(
                "pending" if assumed else "skip",
                name=src.name,
                size=size,
                reason="baseline_no_output" if assumed else "done_store",
            )
            state[sk] = {
                "size": size,
                "mtime_ns": mtime_ns,
                "done": True,
                "last_result": result,
            }
            return result

    track: dict[str, Any] = {}
    # Carry the "already said this is too small" flag across scans, along with
    # the size it was last seen at. The poll backend builds a fresh track every
    # cycle, so without the flag it re-emits `ignored` for the same file
    # forever, and without the size it cannot tell a file parked below the
    # floor from one growing towards it — the first deserves silence, the
    # second a progress line.
    carried = state.get(sk, {})
    if carried.get("said_small"):
        track["said_small"] = True
        if carried.get("small_size") is not None:
            track["prev"] = carried["small_size"]

    if not wait:
        for _ in range(stable_needed + 5):
            status = settle_step(
                src, track, min_bytes=min_bytes, stable_needed=stable_needed
            )
            if status == "ready":
                return apply_action(
                    src,
                    action,
                    state=state,
                    stable_needed=stable_needed,
                    done_store=done_store,
                    retries=retries,
                )
            if status == "gone":
                return None
            try:
                # Reuse settle_step's lsof reading rather than paying again.
                if track.get("open"):
                    return None
                size = src.stat().st_size
                if size < min_bytes:
                    # Say so once — copy defaults to --min-bytes 1000000, and a
                    # sub-threshold file otherwise vanishes without a word.
                    # settle_step normally does the announcing; this covers the
                    # case where the file dropped below the floor after it
                    # looked. Either way the flag has to be persisted here:
                    # returning from this branch skips the one at the bottom of
                    # the loop, so the carry-across never happened and every
                    # scan announced the same file again, forever.
                    if not track.get("said_small"):
                        track["said_small"] = True
                        emit(
                            "ignored",
                            name=src.name,
                            size=size,
                            reason="below_min_bytes",
                            min_bytes=min_bytes,
                        )
                    entry = state.setdefault(sk, {})
                    entry["said_small"] = True
                    entry["small_size"] = size
                    return None
            except OSError:
                return None
            time.sleep(settle_interval)
        # Still growing after the whole budget. Leave it for the next scan.
        return None

    while True:
        status = settle_step(
            src, track, min_bytes=min_bytes, stable_needed=stable_needed
        )
        if status == "gone":
            return None
        if status == "ready":
            return apply_action(
                src,
                action,
                state=state,
                stable_needed=stable_needed,
                done_store=done_store,
                retries=retries,
            )
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
    retries: RetryQueue | None = None,
) -> int:
    """Returns how many files ended in a failing result."""
    failures = 0
    pending = 0
    for path in iter_watch_files(src_dir, exts, recursive=recursive):
        result = process_file(
            path,
            action,
            state=state,
            min_bytes=min_bytes,
            stable_needed=stable_needed,
            settle_interval=settle_interval,
            wait=wait,
            done_store=done_store,
            retries=retries,
        )
        if result in FAILED_RESULTS:
            failures += 1
        elif result == "pending":
            pending += 1
    if pending:
        emit(
            "baseline_pending",
            count=pending,
            hint="skipped but no output found — re-run once with --catch-up",
        )
    return failures


def seed_baseline(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    min_bytes: int,
    state: dict[str, dict],
    done_store: DoneStore | None = None,
    recursive: bool = False,
    skip: set[str] | None = None,
) -> list[Path]:
    """Ignore finished files already in SRC. Return paths still being written.

    `skip` holds paths the retry queue already owns. Without it, seeding marks
    them finished-baseline on restart and the queue entry loses the argument —
    the same class of bug as --catch-up being defeated by baseline entries."""
    unsettled: list[Path] = []
    ignored = 0
    probed = 0
    pending: list[Path] = []
    paths = iter_watch_files(src_dir, exts, recursive=recursive)
    now = time.time()

    for path in paths:
        try:
            size, mtime_ns = file_identity(path)
            resolved = path.resolve()
        except OSError:
            continue
        if skip and (str(path) in skip or str(resolved) in skip):
            continue
        # lsof costs 0.4-0.8s a call and this runs before the watcher accepts
        # events, so skip it where we safely can. An old mtime is NOT proof a
        # file is finished — a stalled writer can hold a handle for hours
        # without touching it — so age alone may only exempt a file whose
        # output already exists. Everything else gets probed.
        produced = action.output_exists(path)
        recent = (now - mtime_ns / 1e9) < BASELINE_LSOF_WINDOW
        open_now = 0
        if recent or produced is not True:
            probed += 1
            open_now = is_open(path)
        if size < min_bytes or open_now:
            unsettled.append(resolved)
            continue
        # Skipping is still the default — starting a watcher must never kick
        # off a surprise run over the whole back catalogue. But say out loud
        # which of these were skipped without ever having been processed.
        if produced is False:
            pending.append(path)
        state[_state_key(path)] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "done": True,
            "baseline": True,
        }
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
        pending=len(pending),
        probed=probed,
        note="old finished skipped; only new + active encodes",
        recursive=recursive,
    )
    if pending:
        emit(
            "baseline_pending",
            count=len(pending),
            names=[p.name for p in pending[:5]],
            more=max(0, len(pending) - 5),
            hint="skipped but no output found — re-run once with --catch-up",
        )
    return unsettled


def list_pending(
    src_dir: Path,
    action: Action,
    *,
    exts: list[str] | None,
    min_bytes: int,
    recursive: bool = False,
) -> list[Path]:
    """Files baseline would skip that have no output to show for it.

    Strictly read-only — no state, no done-store, not even an lsof call — so
    it's safe to point at a live folder while a watcher is running on it.
    """
    out: list[Path] = []
    for path in iter_watch_files(src_dir, exts, recursive=recursive):
        try:
            size, _mtime_ns = file_identity(path)
        except OSError:
            continue
        if size < min_bytes:
            continue
        if action.output_exists(path) is False:
            out.append(path)
    return out


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
    retries: RetryQueue | None = None,
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
            skip=retries.paths() if retries is not None else None,
        )
    present = True
    dest_away_logged = False
    while True:
        # Poll recovers on its own — every cycle re-scans — but it used to do
        # so mutely, warning on each scan and never saying it came back.
        alive = src_dir.is_dir()
        if alive and not present:
            emit("src_restored", src=src_dir)
        elif not alive and present:
            emit("src_lost", src=src_dir, action="scanning until it returns")
        present = alive

        if alive:
            if retries is not None:
                dest_away_logged = service_retries(
                    retries,
                    action,
                    state=state,
                    stable_needed=stable_needed,
                    done_store=done_store,
                    dest_away_logged=dest_away_logged,
                )
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
                retries=retries,
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
    retries: RetryQueue | None = None,
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
        def on_created(self, event):
            self._note(event)

        def on_modified(self, event):
            self._note(event)

        def on_moved(self, event):
            if getattr(event, "dest_path", None):

                class _E:
                    is_directory = event.is_directory
                    src_path = event.dest_path

                self._note(_E())

        def _note(self, event):
            if event.is_directory:
                return
            path = Path(event.src_path)
            # Containment is checked against BOTH the literal event path and
            # its resolved form. Resolving first sent a symlink sitting in the
            # watched folder off to its target, whose parent is elsewhere, so
            # the event was dropped — while the poll backend and --once
            # (which stat through the link) processed the same file happily.
            try:
                resolved = path.resolve()
            except OSError:
                return

            def _inside(candidate: Path) -> bool:
                if candidate.parent == src_dir:
                    return True
                return recursive and src_dir in candidate.parents

            if not (_inside(path) or _inside(resolved)):
                return
            if not should_watch_name(path.name, exts):
                return
            # Ignore baseline files unless size changed (re-export).
            # apply_action writes `state` on the main thread under this lock.
            with lock:
                st = state.get(_state_key(path))
            if st and st.get("done") and st.get("baseline"):
                try:
                    stat = path.stat()
                    # A re-export can land at the identical size, so mtime
                    # decides — otherwise the event is dropped in silence.
                    if (
                        stat.st_size == st.get("size")
                        and stat.st_mtime_ns == st.get("mtime_ns")
                    ):
                        return
                except OSError:
                    return
            key = str(path)
            try:
                rel = str(path.relative_to(src_dir))
            except ValueError:
                rel = path.name
            # Fresh content supersedes a queued retry, which holds the old
            # (size, mtime) and would otherwise re-copy stale bytes.
            if retries is not None:
                retries.drop(path)
            with lock:
                already_queued = key in pending
                pending[key] = time.monotonic()
                watching.pop(key, None)
            # FSEvents fires repeatedly for one write; debounce already
            # coalesces the work, so only log the first of a burst.
            if not already_queued:
                emit("event", name=path.name, path=rel)

    def _prime(*, restoring: bool = False) -> None:
        """Catch-up scan or baseline seed — what startup does, reusable on restore.

        Baseline semantics ("ignore what's already finished") are right at
        startup and wrong after a restore: anything that landed while the
        directory was gone is work we haven't done. Catch-up is safe to force
        because the done-store persists across the outage, so files we already
        handled are skipped rather than redone.
        """
        if catch_up or restoring:
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
                skip=retries.paths() if retries is not None else None,
            )
            started = time.monotonic()
            with lock:
                for p in unsettled:
                    pending[str(p)] = started

    def _start_observer():
        obs = Observer()
        obs.schedule(handler, str(src_dir), recursive=recursive)
        obs.start()
        return obs

    handler = Handler()
    observer = _start_observer()
    _prime()

    debounce_s = max(debounce_ms, 50) / 1000.0
    last_src_check = time.monotonic()
    last_retry_check = time.monotonic()
    dest_away_logged = False

    try:
        while True:
            time.sleep(0.05)
            now = time.monotonic()

            # FSEvents simply goes quiet if the directory is deleted or its
            # volume unmounts, so poll for it rather than trusting silence.
            if now - last_src_check >= SRC_CHECK_INTERVAL:
                last_src_check = now
                if not src_dir.is_dir():
                    emit("src_lost", src=src_dir, action="waiting for it to return")
                    observer.stop()
                    observer.join(timeout=5)
                    with lock:
                        pending.clear()
                        watching.clear()
                        state.clear()
                    waited = 0.0
                    while not src_dir.is_dir():
                        time.sleep(SRC_WAIT_INTERVAL)
                        waited += SRC_WAIT_INTERVAL
                        if waited % SRC_WAIT_LOG_INTERVAL < SRC_WAIT_INTERVAL:
                            emit("src_waiting", src=src_dir, waited_s=int(waited))
                    emit("src_restored", src=src_dir, waited_s=int(waited))
                    observer = _start_observer()
                    _prime(restoring=True)
                    last_src_check = time.monotonic()
                    continue

            # Retries come due on wall-clock, and never re-enter the settle
            # loop: the file already settled once, so re-settling would pay
            # another lsof per file per attempt.
            if retries is not None and now - last_retry_check >= RETRY_SCAN_INTERVAL:
                last_retry_check = now

                def _requeue(rpath: Path, _now: float = now) -> None:
                    with lock:
                        pending[str(rpath)] = _now

                dest_away_logged = service_retries(
                    retries,
                    action,
                    state=state,
                    stable_needed=stable_needed,
                    done_store=done_store,
                    lock=lock,
                    requeue=_requeue,
                    dest_away_logged=dest_away_logged,
                )

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
                    stalled = now - track.get("last_change", now)
                    if stalled > STALL_TIMEOUT:
                        # Ask directly rather than inferring from silence.
                        still_open = is_open(path)
                        if not still_open:
                            # The writer let go and no event told us. This is
                            # a complete file we were about to abandon.
                            with lock:
                                watching.pop(key, None)
                            emit(
                                "settled_late",
                                name=path.name,
                                stalled_s=int(stalled),
                                note="writer released without an event",
                            )
                            apply_action(
                                path,
                                action,
                                state=state,
                                stable_needed=stable_needed,
                                done_store=done_store,
                                lock=lock,
                                retries=retries,
                            )
                            continue

                        held = track.setdefault("stalled_since", now)
                        if now - held > STALL_ABANDON_AFTER:
                            with lock:
                                watching.pop(key, None)
                            emit(
                                "give_up",
                                name=path.name,
                                reason="held_open_too_long",
                                stalled_s=int(now - held),
                                note="--pending will still report it",
                            )
                            continue
                        # Still held: keep it, just stop checking so often.
                        # Forgetting it here is what abandoned finished work.
                        track["last_change"] = now
                        with lock:
                            if key in watching:
                                watching[key]["next_check"] = (
                                    now + STALL_RECHECK_INTERVAL
                                )
                        if not track.get("said_stalled"):
                            track["said_stalled"] = True
                            emit(
                                "stalled",
                                name=path.name,
                                stalled_s=int(stalled),
                                note="writer still holds it; re-checking slowly",
                            )
                        continue
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
                    stable_needed=stable_needed,
                    done_store=done_store,
                    lock=lock,
                    retries=retries,
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
    retries: RetryQueue | None = None,
    watch_name: str | None = None,
    log_path: Path | None = None,
    preset: str | None = None,
    to: str | None = None,
    exclusive: bool = False,
    dry_run: bool = False,
) -> int:
    src_dir = src_dir.expanduser().resolve()
    name = watch_name or preset or f"{action.name}-{src_dir.name}"

    # A source that isn't there is a typo or an unmounted volume, not an empty
    # folder. Reporting "done failed=0" made those indistinguishable.
    if not src_dir.is_dir():
        emit("error", reason="src_missing", src=src_dir)
        return 2

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
        done_store=(str(done_store.path) if done_store is not None else None),
        retry_queue=(len(retries) if retries is not None else None),
    )

    if catch_up and done_store is not None:
        cleared = done_store.drop_baseline()
        if cleared:
            emit("catch_up", cleared_baseline=cleared)

    prev_term = None
    # --dry-run is documented as "no writes". A pidfile is a write, and it also
    # made a preview run collide with --exclusive.
    if not once and not dry_run:
        others = live_watchers(name)
        if others:
            if exclusive:
                emit(
                    "error",
                    reason="already_running",
                    name=name,
                    other_pids=others,
                )
                return 2
            # Warning, not refusal, by default: under launchd KeepAlive a
            # watcher that refuses to start would flap instead of running, and
            # for unattended work that is worse than duplicated copies.
            emit(
                "warn",
                reason="duplicate_watcher",
                name=name,
                other_pids=others,
                note="both will act on the same files; --exclusive refuses",
            )
        # launchd stops services with SIGTERM, whose default disposition kills
        # the process outright — the finally below never runs and the pidfile
        # is left behind looking stale. Turning it into SystemExit unwinds.
        def _on_term(_signum, _frame):
            raise SystemExit(0)

        try:
            prev_term = signal.signal(signal.SIGTERM, _on_term)
        except ValueError:
            prev_term = None  # not the main thread; nothing to install

    try:
        # Inside the try, not before it. The handler above turns SIGTERM into
        # SystemExit, and a signal arriving *during* this write — launchd
        # stopping a service moments after starting it — raised out of a frame
        # the cleanup below did not cover, orphaning the very pidfile it was
        # written to manage. The window is small and entirely reproducible.
        if not once and not dry_run:
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
        if once:
            state: dict[str, dict] = {}
            failures = scan_once(
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
            emit("done", failed=failures)
            # Exit non-zero so a cron or CI wrapper can tell. Nothing-matched
            # is not a failure, so an empty run still exits 0.
            return 1 if failures else 0

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
                retries=retries,
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
            retries=retries,
        )
    finally:
        if not once and not dry_run:
            clear_pidfile(name)
            if prev_term is not None:
                try:
                    signal.signal(signal.SIGTERM, prev_term)
                except ValueError:
                    pass
