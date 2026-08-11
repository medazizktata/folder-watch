"""CLI: argparse + main."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from folder_watch._version import __version__
from folder_watch.actions import ACTION_NAMES, ACTIONS
from folder_watch.actions.base import Action
from folder_watch.actions.convx import build_convx_watch_cmd, run_convx_watch
from folder_watch.actions.copy import CopyAction
from folder_watch.actions.unzip import UnzipAction
from folder_watch.core.util import parse_exts
from folder_watch.core.watch import list_pending, run_watch
from folder_watch.presets import load_presets, save_preset
from folder_watch.support.launchd import (
    install_service,
    service_status,
    uninstall_service,
)
from folder_watch.support.log import set_json_mode, set_verbose, setup_logging
from folder_watch.support.paths import PRESETS_PATH, STATE_DIR
from folder_watch.support.pidfile import (
    clear_pidfile,
    collect_watch_status,
    prune_pidfiles,
    write_pidfile,
)
from folder_watch.support.state import DoneStore


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
            "  copy   SRC --to DEST          # defaults: --ext mp4 --min-bytes 1000000\n"
            "  unzip  SRC [--mode named|same|to] [--to DIR]\n"
            "  convx  SRC --format EXT [--ext png,jpg]   # → convx watch\n"
            "  service install|uninstall|status --preset NAME\n"
            "  status [--preset NAME]   # running watchers\n"
            "  prune [--yes]            # drop dead done-state and stale pidfiles\n"
        ),
    )
    p.add_argument("action", nargs="?", help="Action or 'service'")
    p.add_argument("src", nargs="?", help="Folder to watch (or service subcommand)")
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
    p.add_argument(
        "--ext",
        default=None,
        help="Extensions, or * (default: copy=mp4, unzip=zip)",
    )
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
    p.add_argument(
        "--min-bytes",
        type=int,
        default=None,
        help="ignore files smaller than this (default: copy=1000000, unzip=1)",
    )
    p.add_argument(
        "--once", action="store_true", help="copy/unzip: process ready files and exit"
    )
    p.add_argument(
        "--catch-up",
        action="store_true",
        help="also process files already finished in SRC (default: new/in-progress only)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="log would_copy/would_extract without writing or deleting",
    )
    p.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="macOS notification on copy success/fail (default: on)",
    )
    p.add_argument(
        "--log",
        type=Path,
        default=None,
        help="also write logs to this file",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log level (default INFO; --verbose → DEBUG unless set)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose: DEBUG level + frequent progress/settle lines",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON lines on stdout",
    )
    p.add_argument(
        "--no-done-store",
        action="store_true",
        help="disable persistent done-state (scheme B)",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=5,
        help="copy: retries on OSError (default 5)",
    )
    p.add_argument(
        "--pending",
        action="store_true",
        help=(
            "list files a fresh watcher would skip that have no output, then "
            "exit. Read-only — safe against a folder being watched right now"
        ),
    )
    p.add_argument(
        "--no-clobber",
        action="store_true",
        help="unzip: refuse an archive that would overwrite existing files",
    )
    p.add_argument(
        "--create-dest",
        action="store_true",
        help=(
            "create the full destination path if missing. Off by default: on an "
            "unmounted drive that silently rebuilds the tree on local disk"
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"folder-watch {__version__}",
    )
    p.add_argument("--preset", help="folder-watch JSON preset")
    p.add_argument("--list-presets", action="store_true")
    p.add_argument("--list-actions", action="store_true")
    p.add_argument("--save-preset", metavar="NAME")
    return p


def build_action(
    name: str, args: argparse.Namespace, *, src_root: Path | None = None
) -> Action:
    dry = getattr(args, "dry_run", False)
    if name == "copy":
        if not args.to:
            raise SystemExit("error: copy requires --to DEST")
        return CopyAction(
            Path(args.to),
            notify_on_done=getattr(args, "notify", True) and not dry,
            retries=getattr(args, "retries", 5),
            dry_run=dry,
            create_dest=getattr(args, "create_dest", False),
            # Needed to mirror subdirectories under --to; without it a
            # --recursive watch flattens same-named files onto each other.
            src_root=src_root,
        )
    if name == "unzip":
        return UnzipAction(
            mode=args.mode,
            to=Path(args.to) if args.to else None,
            dry_run=dry,
            create_dest=getattr(args, "create_dest", False),
            no_clobber=getattr(args, "no_clobber", False),
        )
    raise SystemExit(f"error: unknown action {name!r}")


def run_status_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="folder-watch status")
    ap.add_argument("--preset", default=None, help="filter to one preset/watch name")
    ap.add_argument("--tail", type=int, default=12, help="log lines to show")
    args = ap.parse_args(argv)
    rows = collect_watch_status(preset=args.preset, log_tail=args.tail)
    if not rows:
        print("no watchers (no pidfiles)")
        return 1
    alive_any = False
    for row in rows:
        alive = bool(row.get("alive"))
        alive_any = alive_any or alive
        flag = "RUNNING" if alive else "STOPPED"
        print(f"[{flag}] {row.get('name')} pid={row.get('pid')}")
        for k in ("preset", "action", "src", "to", "log", "pidfile", "note"):
            if row.get(k) is not None:
                print(f"  {k}: {row[k]}")
        tail = row.get("tail") or []
        if tail:
            print("  log_tail:")
            for ln in tail:
                print(f"    {ln}")
        print()
    return 0 if alive_any else 1


def run_prune_cli(argv: list[str]) -> int:
    """Clear out state that no longer refers to anything real."""
    ap = argparse.ArgumentParser(prog="folder-watch prune")
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry)")
    args = ap.parse_args(argv)
    dry = not args.yes

    stores = sorted(STATE_DIR.glob("done-*.json"))
    dropped_entries = 0
    emptied: list[Path] = []
    for path in stores:
        store = DoneStore(path)
        before = len(store)
        gone = len(store.dead_keys()) if dry else store.prune()
        dropped_entries += gone
        if gone == before and before > 0:
            emptied.append(path)

    if not dry:
        for path in emptied:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    pidfiles = prune_pidfiles(dry_run=dry)

    verb = "would remove" if dry else "removed"
    print(f"done-state: {len(stores)} store(s), {verb} {dropped_entries} dead entr(ies)")
    if emptied:
        print(f"  {verb} {len(emptied)} store file(s) left with nothing in them:")
        for p in emptied:
            print(f"    {p.name}")
    print(f"pidfiles: {verb} {len(pidfiles)} stale")
    for p in pidfiles:
        print(f"    {p.name}")
    if dry:
        print("\ndry run — re-run with --yes to apply")
    return 0


def run_service_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="folder-watch service")
    ap.add_argument("cmd", choices=("install", "uninstall", "status"))
    ap.add_argument("--preset", required=True, help="preset name to run under launchd")
    ap.add_argument("--log", type=Path, default=None, help="LaunchAgent stdout/err log")
    args = ap.parse_args(argv)
    bin_path = Path(__file__).resolve().parents[1] / "bin" / "folder-watch"
    if not bin_path.is_file():
        bin_path = Path(sys.executable)
    try:
        if args.cmd == "install":
            plist = install_service(
                preset=args.preset, program=bin_path, log_path=args.log
            )
            print(f"installed {plist}")
            return 0
        if args.cmd == "uninstall":
            plist = uninstall_service(preset=args.preset)
            print(f"uninstalled {plist}" if plist else "nothing to uninstall")
            return 0
        st = service_status(preset=args.preset)
        for k, v in st.items():
            print(f"{k}: {v}")
        return 0 if st.get("loaded") or st.get("plist_exists") else 1
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "service":
        return run_service_cli(argv[1:])
    if argv and argv[0] == "status":
        return run_status_cli(argv[1:])
    if argv and argv[0] == "prune":
        return run_prune_cli(argv[1:])

    args = build_parser().parse_args(argv)
    level = args.log_level
    if args.verbose and "--log-level" not in {a.split("=")[0] for a in argv}:
        level = "DEBUG"
    setup_logging(level=level, log_file=args.log, json_mode=args.json)
    set_json_mode(args.json)
    set_verbose(args.verbose)

    presets = load_presets()
    passed = {a.split("=")[0] for a in argv}

    if args.list_actions:
        print("copy  — copy finished files to --to DIR (never deletes source)")
        print("unzip — extract .zip; delete only after verified success")
        print("         --mode named (default) | same | to")
        print(
            "convx — wrapper: exec `convx watch` "
            "(in-place convert; ConvX owns FSEvents)"
        )
        print("         --format EXT and/or --convx-preset; --ext → --filter")
        print("service install|uninstall|status --preset NAME  — LaunchAgent")
        print("status  [--preset NAME]  — running watchers (pidfile + log tail)")
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

    if action_name not in ACTION_NAMES:
        print(f"error: unknown action {action_name!r}", file=sys.stderr)
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

        watch_name = args.preset or f"convx-{src.name}"
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
        # convx never went through run_watch, so it never wrote a pidfile and
        # was invisible to `folder-watch status`.
        write_pidfile(
            watch_name,
            {
                "name": watch_name,
                "preset": args.preset,
                "src": str(src.expanduser()),
                "action": "convx",
                "to": None,
                "log": str(args.log) if args.log else None,
            },
        )
        try:
            return run_convx_watch(cmd)
        finally:
            clear_pidfile(watch_name)

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
        action = build_action(action_name, args, src_root=src)
    except (SystemExit, ValueError) as e:
        msg = e.args[0] if getattr(e, "args", None) else e
        print(msg, file=sys.stderr)
        return 2

    exts = parse_exts(exts_raw)

    if args.pending:
        src_dir = src.expanduser().resolve()
        rows = list_pending(
            src_dir,
            action,
            exts=exts,
            min_bytes=min_bytes,
            recursive=recursive,
        )
        if not rows:
            print(f"nothing pending in {src_dir}")
            return 0
        print(f"{len(rows)} file(s) in {src_dir} that a fresh watcher would skip")
        print("without any output to show for it:\n")
        for path in rows:
            try:
                mb = path.stat().st_size / 1024 / 1024
                print(f"  {mb:9.1f} MB  {path.relative_to(src_dir)}")
            except (OSError, ValueError):
                print(f"  {'?':>9}     {path}")
        print("\nre-run once with --catch-up to process them")
        return 1

    done_store = None
    if not args.no_done_store and action_name in ("copy", "unzip"):
        to_s = str(Path(to).expanduser()) if to else None
        mode_s = mode if action_name == "unzip" else None
        done_store = DoneStore(
            DoneStore.path_for(action_name, src, to=to_s, mode=mode_s)
        )

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
            done_store=done_store,
            watch_name=(args.preset or f"{action_name}-{src.name}"),
            log_path=args.log,
            preset=args.preset,
            to=to,
        )
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
