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
from folder_watch.actions.exec_cmd import ExecAction
from folder_watch.actions.unzip import UnzipAction
from folder_watch.core.util import parse_exts
from folder_watch.core.watch import list_pending, run_watch
from folder_watch.presets import (
    load_presets,
    load_raw_preset,
    preset_sources,
    preset_status,
    remove_preset,
    save_preset,
)
from folder_watch.support.launchd import (
    install_service,
    service_plist_path,
    service_status,
    uninstall_service,
)
from folder_watch.support.log import set_json_mode, set_verbose, setup_logging
from folder_watch.support.notify import DEFAULT_SOUND, available_sounds
from folder_watch.support.paths import PRESETS_PATH, STATE_DIR
from folder_watch.support.pidfile import (
    clear_pidfile,
    collect_watch_status,
    live_watchers,
    prune_pidfiles,
    write_pidfile,
)
from folder_watch.support.retry import GIVE_UP_AFTER, RetryQueue, retry_path_for
from folder_watch.support.state import DoneStore
from folder_watch.support.webhook import configure as configure_webhook


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="folder-watch",
        description=(
            "Watch a folder for finished files, then run an action "
            "(copy, unzip, exec), or wrap another tool that watches for "
            "itself (convx)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "actions:\n"
            "  copy   SRC --to DEST          # defaults: --ext mp4 --min-bytes 1000000\n"
            "  unzip  SRC [--mode named|same|to] [--to DIR]\n"
            "  exec   SRC --cmd 'COMMAND {path}'       # any command, per file\n"
            "  convx  SRC --format EXT [--ext png,jpg]   # → convx watch\n"
            "  service install|uninstall|status --preset NAME\n"
            "  status [--preset NAME]   # running watchers\n"
            "  prune [--yes]            # drop dead done-state and stale pidfiles\n"
            "  presets show NAME            # one preset, with its source files\n"
            "  presets forget NAME [--yes]  # remove a preset\n"
        ),
    )
    g_act = p.add_argument_group('actions: copy / unzip')
    g_exec = p.add_argument_group('actions: exec')
    g_convx = p.add_argument_group('actions: convx (wraps another tool)')
    g_watch = p.add_argument_group('what to watch, and when a file counts as finished')
    g_run = p.add_argument_group('how to run')
    g_tell = p.add_argument_group('being told about it')
    g_log = p.add_argument_group('logging')
    g_state = p.add_argument_group('state and retries')
    g_pre = p.add_argument_group('presets')
    p.add_argument("action", nargs="?", help="Action or 'service'")
    p.add_argument("src", nargs="?", help="Folder to watch (or service subcommand)")
    g_act.add_argument(
        "--to", dest="to", help="Destination / output directory (copy/unzip)"
    )
    g_act.add_argument(
        "--mode",
        choices=("named", "same", "to"),
        default="named",
        help="unzip: named|same|to",
    )
    g_convx.add_argument("--format", dest="format", help="convx watch -t output format")
    g_convx.add_argument("--convx-preset", dest="convx_preset", help="ConvX -p preset")
    g_convx.add_argument("--quality", type=int, help="convx quality 0-100")
    g_convx.add_argument("--width", type=int, help="convx width")
    g_convx.add_argument("--fps", type=float, help="convx fps")
    g_convx.add_argument("--max-size", type=int, dest="max_size", help="convx max bytes")
    g_convx.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="convx --overwrite (default true)",
    )
    g_exec.add_argument(
        "--cmd",
        default=None,
        metavar="COMMAND",
        help=(
            "exec: command to run per settled file. {path} {dir} {name} "
            "{stem} {ext} {rel} are substituted. No shell — exit 0 done, "
            "75 retry later, other fail"
        ),
    )
    p.add_argument(
        "--exec-timeout",
        type=float,
        default=3600.0,
        dest="exec_timeout",
        metavar="SECONDS",
        help=(
            "exec: kill the command after this long (default 3600, 0 = never). "
            "A command that hangs stops the watcher silently"
        ),
    )
    g_convx.add_argument("--convx-bin", default=None, help="Path to convx")
    g_convx.add_argument(
        "--passthru",
        default=None,
        metavar="ARGS",
        help='convx: extra args appended verbatim, e.g. --passthru "--new-flag 3"',
    )
    g_watch.add_argument(
        "--ext",
        default=None,
        help="Extensions, or * (default: copy=mp4, unzip=zip)",
    )
    g_watch.add_argument("--poll", type=float, default=30, help="poll backend interval")
    g_watch.add_argument(
        "--backend",
        choices=("event", "poll"),
        default="event",
        help="copy/unzip: event=FSEvents (default); poll=timer",
    )
    g_watch.add_argument(
        "--debounce",
        type=int,
        default=500,
        dest="debounce_ms",
        help="quiet window ms (default 500); passed to convx watch",
    )
    g_watch.add_argument(
        "--settle",
        type=float,
        default=0.5,
        help="copy/unzip: seconds between size/lsof checks after event",
    )
    g_watch.add_argument(
        "--recursive", action="store_true", help="copy/unzip: watch subdirs"
    )
    g_watch.add_argument(
        "--stable",
        type=int,
        default=3,
        help="copy/unzip: same-size checks before action (default 3)",
    )
    g_watch.add_argument(
        "--min-bytes",
        type=int,
        default=None,
        help="ignore files smaller than this (default: copy=1000000, unzip=1)",
    )
    g_run.add_argument(
        "--once", action="store_true", help="copy/unzip: process ready files and exit"
    )
    g_run.add_argument(
        "--catch-up",
        action="store_true",
        help="also process files already finished in SRC (default: new/in-progress only)",
    )
    g_run.add_argument(
        "--dry-run",
        action="store_true",
        help="log would_copy/would_extract without writing or deleting",
    )
    g_tell.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="macOS notification on copy success/fail (default: on)",
    )
    g_tell.add_argument(
        "--sound",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="play a sound with the notification (default: on; --no-sound off)",
    )
    p.add_argument(
        "--sound-name",
        default=DEFAULT_SOUND,
        dest="sound_name",
        metavar="NAME",
        help=(
            f"which macOS sound to play (default {DEFAULT_SOUND} — short and "
            "quiet). --list-sounds shows what this machine has"
        ),
    )
    g_tell.add_argument(
        "--list-sounds",
        action="store_true",
        help="print the sound names available on this machine, then exit",
    )
    g_log.add_argument(
        "--log",
        type=Path,
        default=None,
        help="also write logs to this file",
    )
    g_log.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log level (default INFO; --verbose → DEBUG unless set)",
    )
    g_log.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose: DEBUG level + frequent progress/settle lines",
    )
    g_log.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON lines on stdout",
    )
    g_state.add_argument(
        "--no-done-store",
        action="store_true",
        help="disable persistent done-state (scheme B)",
    )
    p.add_argument(
        "--webhook",
        default=None,
        metavar="URL",
        help=(
            "POST outcome events (copied/extracted/ran/fail/...) as JSON to "
            "this URL. Never blocks the watch loop; the URL is not logged"
        ),
    )
    g_act.add_argument(
        "--verify",
        action="store_true",
        help=(
            "copy: re-read both files and compare sha256 after copying. Costs "
            "a full read of each; catches the right number of wrong bytes, "
            "which the size check cannot"
        ),
    )
    g_act.add_argument(
        "--retries",
        type=int,
        default=5,
        help="copy: retries on OSError (default 5)",
    )
    g_run.add_argument(
        "--pending",
        action="store_true",
        help=(
            "list files a fresh watcher would skip that have no output, then "
            "exit. Read-only — safe against a folder being watched right now"
        ),
    )
    p.add_argument(
        "--retry-hours",
        type=float,
        default=GIVE_UP_AFTER / 3600,
        help=(
            "give up on a transiently-failed file after this long (default 72). "
            "Covers a weekend with the machine asleep; expiry is recoverable, "
            "not destructive — --pending and --catch-up still see the file"
        ),
    )
    g_run.add_argument(
        "--exclusive",
        action="store_true",
        help=(
            "refuse to start if another watcher already holds this name. Off by "
            "default: under launchd a refusing service flaps instead of running"
        ),
    )
    g_act.add_argument(
        "--no-clobber",
        action="store_true",
        help="unzip: refuse an archive that would overwrite existing files",
    )
    g_act.add_argument(
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
    g_pre.add_argument("--preset", help="folder-watch JSON preset")
    g_pre.add_argument("--list-presets", action="store_true")
    g_pre.add_argument("--list-actions", action="store_true")
    g_pre.add_argument("--save-preset", metavar="NAME")
    return p


def explicit_dests(argv: list[str]) -> set[str]:
    """Which settings the user actually typed, by argparse dest.

    --save-preset used to rebuild the whole config from resolved values, so
    re-saving to change one field silently reset every other one to its
    default — ext back to mp4, min_bytes back to 1000000, no_clobber gone —
    while reporting success. Knowing what was typed turns that into a patch.
    """
    passed = {a.split("=")[0] for a in argv}
    out: set[str] = set()
    for action in build_parser()._actions:
        if action.option_strings and any(o in passed for o in action.option_strings):
            out.add(action.dest)
    return out


def persist_preset(
    name: str,
    cfg: dict[str, Any],
    *,
    argv: list[str],
    loaded_preset: str | None,
) -> None:
    """Write a preset, merging rather than replacing, and say what changed."""
    existing = load_raw_preset(name)

    if existing is None:
        final = dict(cfg)
    elif loaded_preset:
        # Values came from a preset the user deliberately loaded, so the
        # resolved config is intentional in full.
        final = {**existing, **cfg}
    else:
        # Only what was typed, plus the identity of the watch itself.
        keep = explicit_dests(argv) | {"action", "src"}
        final = {**existing, **{k: v for k, v in cfg.items() if k in keep}}

    if existing is not None:
        print(f"note: preset {name!r} already exists — modifying it, not creating it")
        changes = [
            (k, existing.get(k, "<unset>"), v)
            for k, v in final.items()
            if existing.get(k) != v
        ]
        removed = [k for k in existing if k not in final]
        if not changes and not removed:
            print(f"  no changes; {name!r} left as it was")
            return
        for key, before, after in changes:
            print(f"  {key}: {before} -> {after}")
        for key in removed:
            print(f"  {key}: {existing[key]} -> <removed>")

    save_preset(name, final)
    verb = "updated" if existing is not None else "saved"
    print(f"{verb} preset {name!r} -> {PRESETS_PATH}")


def run_presets_show(name: str) -> int:
    """One preset in full: what it sets, where it came from, whether it works.

    --list-presets prints every preset, which is the wrong tool when you have
    a dozen and a question about one of them. This also answers "why is it not
    doing what I told it to", by naming the file each key came from — a preset
    defined twice is merged, and until now nothing said so for a single name.
    """
    presets = load_presets()
    if name not in presets:
        print(f"error: no preset named {name!r}", file=sys.stderr)
        print(f"known: {', '.join(sorted(presets)) or '(none)'}", file=sys.stderr)
        return 1

    cfg = presets[name]
    level, note = preset_status(cfg)
    print(f"{name}:" + (f"    [{note}]" if note else ""))
    for key in sorted(cfg):
        print(f"  {key}: {cfg[key]}")

    sources = preset_sources(name)
    for path in sources:
        print(f"  defined in: {path}")
    if len(sources) > 1:
        print("  -> defined more than once; later files win key by key")

    running = live_watchers(name)
    print(f"  running: {running}" if running else "  running: no")
    plist = service_plist_path(name)
    print(f"  launchagent: {plist if plist.is_file() else 'not installed'}")
    return 0 if level == "ok" else 1


def run_presets_cli(argv: list[str]) -> int:
    """Show or remove a preset — never as a side effect of anything else."""
    ap = argparse.ArgumentParser(prog="folder-watch presets")
    ap.add_argument("cmd", choices=("show", "forget"))
    ap.add_argument("name", help="preset to show or forget")
    ap.add_argument("--yes", action="store_true", help="actually remove (default: dry)")
    args = ap.parse_args(argv)

    name = args.name
    if args.cmd == "show":
        return run_presets_show(name)
    sources = preset_sources(name)
    if not sources:
        print(f"error: no preset named {name!r}", file=sys.stderr)
        known = ", ".join(sorted(load_presets())) or "(none)"
        print(f"known: {known}", file=sys.stderr)
        return 1

    # Pulling config out from under a running watcher is not a tidy-up.
    running = live_watchers(name)
    if running:
        print(
            f"error: {name!r} is in use by running watcher(s) {running}. "
            "Stop it first.",
            file=sys.stderr,
        )
        return 2

    print(f"preset {name!r}")
    for path in sources:
        print(f"  defined in: {path}")
    if len(sources) > 1:
        print("    -> defined more than once; all copies will be removed")
    plist = service_plist_path(name)
    if plist.is_file():
        print(f"  LaunchAgent installed: {plist}")
        print("    -> run `folder-watch service uninstall --preset "
              f"{name}` first, or it will fail on next login")
    if not args.yes:
        print("\ndry run — re-run with --yes to remove")
        return 0

    changed = remove_preset(name)
    if changed:
        for path in changed:
            print(f"\nremoved {name!r} from {path}")
        return 0
    print(f"\nnothing stored to remove for {name!r}", file=sys.stderr)
    return 1


def chosen_sound(args: argparse.Namespace) -> str | None:
    """The sound to pass an action, or None for a silent notification."""
    if not getattr(args, "sound", True):
        return None
    return getattr(args, "sound_name", None) or DEFAULT_SOUND


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
            sound=chosen_sound(args),
            retries=getattr(args, "retries", 5),
            dry_run=dry,
            create_dest=getattr(args, "create_dest", False),
            verify=getattr(args, "verify", False),
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
            # mode=to needs this to mirror subdirectories; without it a
            # --recursive watch collapses same-stemmed archives onto each other
            # and then deletes both.
            src_root=src_root,
            notify_on_done=getattr(args, "notify", True) and not dry,
            sound=chosen_sound(args),
        )
    if name == "exec":
        if not getattr(args, "cmd", None):
            raise SystemExit("error: exec requires --cmd 'COMMAND {path}'")
        try:
            return ExecAction(
                args.cmd,
                src_root=src_root,
                timeout=getattr(args, "exec_timeout", 3600.0),
                dry_run=dry,
                # Off by default, unlike copy: a command action can fire on
                # every file in a busy folder, and a notification per file is
                # not a feature. --notify opts in.
                notify_on_done="--notify" in getattr(args, "_passed", ()) and not dry,
                sound=chosen_sound(args),
            )
        except ValueError as e:
            raise SystemExit(f"error: {e}") from None
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

    # ".json" not ".json.tmp": a crash during a save leaves an unpublished
    # temp beside the real file, and it used to match this glob — so prune
    # read somebody's half-written debris as a store and tidied it as one.
    stores = sorted(
        p for p in STATE_DIR.glob("done-*.json") if not p.name.endswith(".json.tmp")
    )
    orphan_temps = sorted(STATE_DIR.glob("*.json.tmp"))
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
    if orphan_temps:
        # Left by a process killed mid-save. The real file is intact — this is
        # the copy that never got published — so it is pure debris.
        print(f"  {verb} {len(orphan_temps)} unpublished temp file(s) from a crash")
        for t in orphan_temps:
            if not dry:
                try:
                    t.unlink(missing_ok=True)
                except OSError:
                    pass
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
            removed = uninstall_service(preset=args.preset)
            print(f"uninstalled {removed}" if removed else "nothing to uninstall")
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
    if argv and argv[0] == "presets":
        return run_presets_cli(argv[1:])

    args = build_parser().parse_args(argv)
    level = args.log_level
    if args.verbose and "--log-level" not in {a.split("=")[0] for a in argv}:
        level = "DEBUG"
    setup_logging(level=level, log_file=args.log, json_mode=args.json)
    set_json_mode(args.json)
    set_verbose(args.verbose)
    if getattr(args, "webhook", None):
        configure_webhook(args.webhook)

    presets = load_presets()
    passed = {a.split("=")[0] for a in argv}
    # Which flags the user actually typed, as opposed to which have a default.
    # BooleanOptionalAction cannot tell those apart on its own, and for exec
    # the difference decides whether every processed file raises a notification.
    args._passed = frozenset(passed)

    if args.list_sounds:
        names = available_sounds()
        print("\n".join(names) if names else "no sounds found")
        return 0

    if args.list_actions:
        print("copy  — copy finished files to --to DIR (never deletes source)")
        print("exec  — run any command per settled file ({path} etc. substituted)")
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
        # Annotate rather than act: a listing that renders a dead preset
        # identically to a live one is the whole complaint, and deleting one
        # is a statement about your project that this tool cannot make.
        if not presets:
            # Silence reads as "the command did nothing" rather than "there is
            # nothing to show", which is the same confusion baseline_pending
            # and `ignored` exist to clear up elsewhere.
            print(f"no presets configured — none found in {PRESETS_PATH}")
            return 0
        broken = 0
        for name, cfg in presets.items():
            level, note = preset_status(cfg)
            if level != "ok":
                broken += 1
            print(f"{name}:" + (f"    [{note}]" if note else ""))
            for k, v in cfg.items():
                print(f"  {k}: {v}")
        if broken:
            print(
                f"\n{broken} of {len(presets)} preset(s) need attention. "
                "Nothing was changed — fix the path, or remove the preset "
                f"from {PRESETS_PATH}"
            )
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
    passthru = args.passthru
    backend = args.backend
    debounce_ms = args.debounce_ms
    settle = args.settle
    recursive = args.recursive
    # Which keys the preset itself defined, as opposed to what a flag default
    # left in place. --once needs the difference: a preset is configuration the
    # user wrote down, not a default to be overridden.
    preset_supplied: set[str] = set()

    if args.preset:
        if args.preset not in presets:
            print(f"error: unknown preset {args.preset!r}", file=sys.stderr)
            print("known:", ", ".join(presets) or "(none)", file=sys.stderr)
            return 2
        cfg = presets[args.preset]
        preset_supplied = set(cfg)
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
        if "--passthru" not in passed and cfg.get("passthru"):
            passthru = cfg["passthru"]
            args.passthru = passthru
        if "--sound" not in passed and "--no-sound" not in passed \
                and cfg.get("sound") is not None:
            args.sound = bool(cfg["sound"])
        if "--sound-name" not in passed and cfg.get("sound_name"):
            args.sound_name = str(cfg["sound_name"])
        if "--webhook" not in passed and cfg.get("webhook"):
            args.webhook = str(cfg["webhook"])
            configure_webhook(args.webhook)
        if "--cmd" not in passed and cfg.get("cmd"):
            args.cmd = cfg["cmd"]
        if "--exec-timeout" not in passed and cfg.get("exec_timeout") is not None:
            args.exec_timeout = float(cfg["exec_timeout"])
        # Flags added after presets were designed. A watcher started with
        # --preset had no way to reach them at all, so safety options like
        # --no-clobber were unavailable to exactly the unattended runs that
        # most need them.
        for flag, key in (
            ("--no-clobber", "no_clobber"),
            ("--create-dest", "create_dest"),
            ("--exclusive", "exclusive"),
            ("--no-done-store", "no_done_store"),
            ("--verify", "verify"),
        ):
            if flag not in passed and cfg.get(key):
                setattr(args, key, True)
        if "--retry-hours" not in passed and cfg.get("retry_hours") is not None:
            args.retry_hours = float(cfg["retry_hours"])

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
    args.passthru = passthru

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
            convx_cfg: dict[str, Any] = {
                "action": "convx",
                "src": str(src.expanduser()),
                "debounce_ms": debounce_ms,
                "overwrite": overwrite,
                "ext": [] if exts_raw.strip() == "*" else list(filter_exts or []),
            }
            if fmt:
                convx_cfg["format"] = fmt
            if convx_preset:
                convx_cfg["convx_preset"] = convx_preset
            if quality is not None:
                convx_cfg["quality"] = quality
            if width is not None:
                convx_cfg["width"] = width
            if fps is not None:
                convx_cfg["fps"] = fps
            if max_size is not None:
                convx_cfg["max_size"] = max_size
            if convx_bin:
                convx_cfg["convx_bin"] = convx_bin
            if passthru:
                convx_cfg["passthru"] = passthru
            persist_preset(
                args.save_preset, convx_cfg, argv=argv, loaded_preset=args.preset
            )
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
                passthru=passthru,
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
        if action_name == "exec":
            cfg["cmd"] = args.cmd
            cfg["exec_timeout"] = args.exec_timeout
        # Round-trip the safety flags too, so a saved preset preserves how the
        # command was actually invoked rather than silently dropping them.
        for key in ("no_clobber", "create_dest", "exclusive", "no_done_store",
                    "verify"):
            if getattr(args, key, False):
                cfg[key] = True
        # Round-trip the sound settings too, so a saved preset sounds the way
        # the command that made it did.
        cfg["sound"] = bool(getattr(args, "sound", True))
        cfg["sound_name"] = getattr(args, "sound_name", None) or DEFAULT_SOUND
        if getattr(args, "retry_hours", None) is not None:
            cfg["retry_hours"] = args.retry_hours
        persist_preset(args.save_preset, cfg, argv=argv, loaded_preset=args.preset)
        return 0

    try:
        action = build_action(action_name, args, src_root=src)
    except (SystemExit, ValueError) as e:
        msg = e.args[0] if getattr(e, "args", None) else e
        print(msg, file=sys.stderr)
        return 2

    exts = parse_exts(exts_raw)

    if args.once:
        # --once used to hardcode these while the start line printed the
        # user's values as if they were in effect. Keep the fast defaults,
        # but honour whatever was actually specified — which includes a
        # preset, not just the command line. Both of these decide when a file
        # counts as finished, so a preset raising them because that encoder
        # needs it must not be quietly dropped for a single pass.
        if "--stable" not in passed and "stable" not in preset_supplied:
            stable = 1
        if "--settle" not in passed and "settle" not in preset_supplied:
            settle = 0.05

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
    retries = None
    # --dry-run is documented as "no writes"; it was still seeding baseline
    # entries into the done-store and writing a pidfile.
    if not args.no_done_store and not args.dry_run and action_name in ("copy", "unzip"):
        # Resolve, so `--to ./dst` and `--to /abs/dst` hash to one store
        # instead of fragmenting the done-state and retry queue in two.
        to_s = str(Path(to).expanduser().resolve()) if to else None
        mode_s = mode if action_name == "unzip" else None
        done_store = DoneStore(
            DoneStore.path_for(action_name, src, to=to_s, mode=mode_s)
        )
        # --once has no loop left to service a queue, so it doesn't get one;
        # a transient failure there surfaces as a non-zero exit instead.
        if not args.once:
            retries = RetryQueue(
                retry_path_for(action_name, src, to=to_s, mode=mode_s),
                give_up_after=args.retry_hours * 3600,
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
            retries=retries,
            watch_name=(args.preset or f"{action_name}-{src.name}"),
            log_path=args.log,
            preset=args.preset,
            to=to,
            exclusive=args.exclusive,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
