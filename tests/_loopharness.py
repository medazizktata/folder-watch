"""Test doubles that make the watch loops deterministic.

The loops in core/watch.py are hard to test for three reasons: they never
return, they measure their most interesting behaviour in minutes and days, and
the event backend only wakes when the filesystem says so. Each of those gets a
stand-in here.

Nothing in here touches real time, real FSEvents or real threads, so the tests
that use it run in milliseconds and cannot flake on a loaded machine.
"""
from __future__ import annotations

import contextlib
import io
from typing import Any, ClassVar


class StopLoop(Exception):
    """Ends a watch loop that has no other exit."""


class Clock:
    """Virtual time. Sleeping advances it instead of blocking.

    Installed over `watch.time`, so only the module under test sees it — the
    real clock keeps working for everything else, including watchdog.

    `sleep` is also the loops' only regular callback, so it doubles as the tick
    hook: `on_tick` is where a test deletes a directory, releases a writer, or
    jumps the clock past a timeout.
    """

    def __init__(
        self,
        *,
        stop_after: int,
        on_tick=None,
        stop_exc: type[BaseException] = StopLoop,
    ) -> None:
        self.now = 10_000.0
        self.wall = 1_700_000_000.0
        self.ticks = 0
        self.stop_after = stop_after
        self.on_tick = on_tick
        self.stop_exc = stop_exc

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds
        self.ticks += 1
        if self.ticks > self.stop_after:
            raise self.stop_exc()
        if self.on_tick is not None:
            self.on_tick(self)

    def jump(self, seconds: float) -> None:
        """Skip ahead without counting a tick — for crossing a timeout."""
        self.now += seconds
        self.wall += seconds


class FakeObserver:
    """Stands in for the FSEvents observer: no threads, no notifications.

    Tests call the captured handler directly, which turns "did watchdog get
    round to telling us?" from a race into a function call.
    """

    made: ClassVar[list[FakeObserver]] = []

    def __init__(self) -> None:
        self.handler: Any = None
        self.root: str | None = None
        self.recursive = False
        self.started = False
        self.stopped = False
        FakeObserver.made.append(self)

    def schedule(self, handler, path, recursive=False):
        self.handler, self.root, self.recursive = handler, path, recursive

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        return None


class Event:
    """The subset of a watchdog event that the handler actually reads."""

    def __init__(self, path, *, is_directory=False, dest=None) -> None:
        self.src_path = str(path)
        self.is_directory = is_directory
        if dest is not None:
            self.dest_path = str(dest)


class OpenScript:
    """A scripted `is_open`, recording the virtual time of every probe.

    lsof is the one thing the settle logic cannot fake for itself, so who asks
    and when is exactly what several of these tests are about.
    """

    def __init__(self, answer=0) -> None:
        self.answer = answer
        self.probes: list[float] = []
        self.clock: Clock | None = None

    def __call__(self, _path) -> int:
        self.probes.append(self.clock.now if self.clock else 0.0)
        if callable(self.answer):
            return self.answer(self)
        return self.answer


@contextlib.contextmanager
def captured(*also):
    """Collect emitted events as (name, fields) instead of printing them.

    Each module binds `emit` at import time, so patching the log module would
    miss every existing caller — the swap has to happen per module. `also`
    takes any extra modules whose output belongs in the same transcript, which
    is how an action's own lines and the loop's end up interleaved in order.
    """
    import folder_watch.core.watch as watch_mod

    log: list[tuple[str, dict]] = []
    targets = (watch_mod, *also)
    saved = [(m, m.emit) for m in targets]
    for module in targets:
        module.emit = lambda event, **fields: log.append((event, fields))
    try:
        yield log
    finally:
        for module, original in saved:
            module.emit = original


@contextlib.contextmanager
def patched(clock=None, is_open=None, *, fake_observer=False):
    """Swap the module's clock, its lsof and (optionally) its observer."""
    import folder_watch.core.watch as watch_mod

    old_time, old_open = watch_mod.time, watch_mod.is_open
    old_observer = None
    if clock is not None:
        watch_mod.time = clock
    if is_open is not None:
        if isinstance(is_open, OpenScript):
            is_open.clock = clock
        watch_mod.is_open = is_open
    if fake_observer:
        import watchdog.observers

        FakeObserver.made.clear()
        old_observer = watchdog.observers.Observer
        watchdog.observers.Observer = FakeObserver
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        watch_mod.time, watch_mod.is_open = old_time, old_open
        if old_observer is not None:
            import watchdog.observers

            watchdog.observers.Observer = old_observer


def kinds(log) -> list[str]:
    return [name for name, _ in log]


def fields(log, event) -> list[dict]:
    return [f for name, f in log if name == event]


def count(log, event) -> int:
    return sum(1 for name, _ in log if name == event)
