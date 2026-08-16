"""Argv shims behind the watch-copy / watch-unzip / watch-convx commands.

Each one rearranges a terse positional form into a full folder-watch invocation:

    watch-copy ./Out ./Drive   ->   folder-watch copy ./Out --to ./Drive

Flags are passed straight through, so anything folder-watch accepts still works.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable

from folder_watch.cli import main as fw_main

# Flags that folder-watch itself handles; hand the whole argv over untouched.
PASSTHROUGH = ("--list-presets", "--list-actions", "--preset")

# Every folder-watch flag that takes a value. A flag missing here has its
# value mistaken for a positional and the shim errors with "unexpected [...]"
# — which is what happened to --log, --retries and --log-level.
_SHARED_VALUE_FLAGS = frozenset(
    {"--ext", "--poll", "--stable", "--min-bytes", "--save-preset",
     "--backend", "--debounce", "--settle", "--log", "--log-level",
     "--retries", "--retry-hours", "--to", "--mode"}
)
COPY_VALUE_FLAGS = _SHARED_VALUE_FLAGS
UNZIP_VALUE_FLAGS = _SHARED_VALUE_FLAGS
CONVX_VALUE_FLAGS = frozenset(
    {"--ext", "--format", "--convx-preset", "--quality", "--width", "--fps",
     "--max-size", "--convx-bin", "--debounce", "--save-preset",
     "--log", "--log-level", "--passthru"}
)


def _is_passthrough(argv: Iterable[str]) -> bool:
    return any(a in PASSTHROUGH or a.startswith("--preset=") for a in argv)


def _split(argv: list[str], value_flags: frozenset[str]) -> tuple[list[str], list[str]]:
    """Separate positionals from flags, keeping each flag with its value.

    `--flag=value` needs no special handling — argparse splits that itself.
    """
    positional: list[str] = []
    flags: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):
            flags.append(a)
            if a in value_flags and i + 1 < len(argv):
                i += 1
                flags.append(argv[i])
            i += 1
            continue
        positional.append(a)
        i += 1
    return positional, flags


def _argv(argv: list[str] | None) -> list[str]:
    return list(sys.argv[1:] if argv is None else argv)


def copy_main(argv: list[str] | None = None) -> int:
    argv = _argv(argv)
    if "--help" in argv or "-h" in argv:
        print(
            "watch-copy → folder-watch copy SRC --to DEST\n"
            "  watch-copy ./Out ./Drive\n"
            "  watch-copy --preset nightly-render"
        )
        return 0
    if _is_passthrough(argv):
        return fw_main(argv)
    positional, flags = _split(argv, COPY_VALUE_FLAGS)
    if len(positional) < 2:
        print("usage: watch-copy SRC DEST [options]", file=sys.stderr)
        return 2
    src, dest, *rest = positional
    if rest:
        print(f"error: unexpected {rest}", file=sys.stderr)
        return 2
    return fw_main(["copy", src, "--to", dest, *flags])


def unzip_main(argv: list[str] | None = None) -> int:
    argv = _argv(argv)
    if "--help" in argv or "-h" in argv:
        print(
            "watch-unzip → folder-watch unzip SRC\n"
            "  watch-unzip ~/Downloads/inbox [--mode named|same|to]"
        )
        return 0
    if _is_passthrough(argv):
        return fw_main(argv)
    positional, flags = _split(argv, UNZIP_VALUE_FLAGS)
    if len(positional) != 1:
        print("usage: watch-unzip SRC [options]", file=sys.stderr)
        return 2
    return fw_main(["unzip", positional[0], *flags])


def convx_main(argv: list[str] | None = None) -> int:
    argv = _argv(argv)
    if "--help" in argv or "-h" in argv:
        print(
            "watch-convx → folder-watch convx → convx watch\n"
            "  watch-convx ~/Inbox --format webp --ext png,jpg\n"
            "  (in-place convert; ConvX owns FSEvents)"
        )
        return 0
    if _is_passthrough(argv):
        return fw_main(argv)
    positional, flags = _split(argv, CONVX_VALUE_FLAGS)
    if len(positional) != 1:
        print(
            "usage: watch-convx SRC --format EXT [--ext png,jpg] [options]",
            file=sys.stderr,
        )
        return 2
    return fw_main(["convx", positional[0], *flags])


MAINS = {"copy": copy_main, "unzip": unzip_main, "convx": convx_main}


def main(argv: list[str] | None = None) -> int:
    """`python -m folder_watch.shortcuts <copy|unzip|convx> ...`

    Lets bin/watch-* work straight from a checkout, with no install.
    """
    argv = _argv(argv)
    if not argv or argv[0] not in MAINS:
        print(f"usage: {' | '.join(MAINS)} ...", file=sys.stderr)
        return 2
    return MAINS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
