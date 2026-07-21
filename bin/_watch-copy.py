#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print("watch-copy → folder-watch copy SRC --to DEST\n  watch-copy ./Out ./Drive\n  watch-copy --preset la-chaise-v1")
        return 0
    if "--list-presets" in argv or "--list-actions" in argv or "--preset" in argv or any(a.startswith("--preset=") for a in argv):
        from folder_watch import main as fw; return fw(argv)
    positional, flags, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):
            flags.append(a)
            if a in ("--ext","--poll","--stable","--min-bytes","--save-preset","--backend","--debounce","--settle") and i+1 < len(argv):
                i += 1; flags.append(argv[i])
            i += 1; continue
        positional.append(a); i += 1
    if len(positional) < 2:
        print("usage: watch-copy SRC DEST [options]", file=sys.stderr); return 2
    src, dest, *rest = positional
    if rest:
        print(f"error: unexpected {rest}", file=sys.stderr); return 2
    from folder_watch import main as fw
    return fw(["copy", src, "--to", dest, *flags])

if __name__ == "__main__":
    raise SystemExit(main())
