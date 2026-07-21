#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(
            "watch-convx → folder-watch convx → convx watch\n"
            "  watch-convx ~/Inbox --format webp --ext png,jpg\n"
            "  (in-place convert; ConvX owns FSEvents)"
        )
        return 0
    if (
        "--list-presets" in argv
        or "--list-actions" in argv
        or "--preset" in argv
        or any(a.startswith("--preset=") for a in argv)
    ):
        from folder_watch import main as fw

        return fw(argv)
    positional, flags, i = [], [], 0
    vf = {
        "--ext",
        "--format",
        "--convx-preset",
        "--quality",
        "--width",
        "--fps",
        "--max-size",
        "--convx-bin",
        "--debounce",
        "--save-preset",
    }
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):
            flags.append(a)
            if a in vf and i + 1 < len(argv):
                i += 1
                flags.append(argv[i])
            i += 1
            continue
        positional.append(a)
        i += 1
    if len(positional) != 1:
        print(
            "usage: watch-convx SRC --format EXT [--ext png,jpg] [options]",
            file=sys.stderr,
        )
        return 2
    from folder_watch import main as fw

    return fw(["convx", positional[0], *flags])


if __name__ == "__main__":
    raise SystemExit(main())
