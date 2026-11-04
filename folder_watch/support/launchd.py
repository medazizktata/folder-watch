"""macOS LaunchAgent install/uninstall."""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from folder_watch.presets import load_presets
from folder_watch.support.paths import LABEL_PREFIX, LAUNCH_AGENTS, STATE_DIR


def stdio_log_path(log_path: Path) -> Path:
    """Where launchd's stdout/stderr go — never the same file as --log.

    Pointing StandardOutPath at the --log file gives that file two writers with
    independent offsets: Python's FileHandler and launchd's redirect. Every
    line lands twice and they overwrite each other mid-line, so the log ends up
    both duplicated and corrupted.
    """
    return log_path.with_name(f"{log_path.stem}.out{log_path.suffix or '.log'}")


def launchagent_path_env() -> str:
    """PATH handed to a watcher started by launchd.

    /usr/sbin is not decoration: lsof lives there, and launchd's default PATH
    omits it. Without it the watcher cannot tell whether a file is still being
    written — and a version of this once treated "lsof missing" as "still
    open", so nothing ever settled and both watchers silently did nothing.

    Exposed so the test suite can assert against the real value rather than a
    copy of it that drifts.
    """
    local_bin = Path.home() / ".local" / "bin"
    return (
        f"{local_bin}:/opt/homebrew/bin:/usr/local/bin"
        ":/usr/bin:/bin:/usr/sbin:/sbin"
    )


def service_label(preset: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in preset)
    return f"{LABEL_PREFIX}.{safe}"


def service_plist_path(preset: str) -> Path:
    return LAUNCH_AGENTS / f"{service_label(preset)}.plist"


def _installed_venv(program: Path) -> Path | None:
    """The venv `program` is installed into, if it is installed into one.

    A venv is identifiable by the pyvenv.cfg beside its bin/ — no guessing
    from directory names. Returns the interpreter, not the directory, because
    that is what the plist needs.
    """
    bin_dir = program.parent
    if bin_dir.name != "bin" or not (bin_dir.parent / "pyvenv.cfg").is_file():
        return None
    for name in ("python", "python3"):
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return None


def _resolve_python_and_root(program: Path) -> tuple[Path, Path | None]:
    """Which interpreter runs the service, and whether it needs a PYTHONPATH.

    A `root` of None means the package is installed into the interpreter's own
    site-packages, so the service must NOT be handed a PYTHONPATH — pointing it
    at a checkout would put that checkout ahead of the installed copy on
    sys.path, and the service would silently go back to running whatever is in
    the working tree. Which is the entire thing an installed copy exists to
    avoid.
    """
    # Installed layout: <venv>/bin/{folder-watch,python}, package in
    # site-packages. Checked BEFORE resolving, and never on a resolved path:
    # a venv's bin/python is a symlink to the base interpreter, so resolve()
    # walks straight out of the venv into Cellar and the layout stops being
    # recognisable. That is the same trap the checkout branch below documents,
    # and it is what put a Homebrew interpreter with no folder_watch on it
    # into a live plist.
    venv_python = _installed_venv(program.expanduser())
    if venv_python is not None:
        return venv_python, None

    program = program.expanduser().resolve()

    # Checkout layout: <root>/bin/folder-watch with <root>/.venv beside it.
    root = (
        program.parent.parent
        if program.name == "folder-watch" and program.parent.name == "bin"
        else program.parent
    )
    # Do NOT Path.resolve() the venv python — that follows the symlink into
    # Cellar and drops venv site-packages (watchdog, etc.).
    for name in ("python", "python3"):
        venv_py = root / ".venv" / "bin" / name
        if venv_py.is_file():
            return venv_py, root
    which = subprocess.run(
        ["python3", "-c", "import sys; print(sys.executable)"],
        capture_output=True,
        text=True,
        check=False,
    )
    py = Path((which.stdout or "").strip() or "/usr/bin/python3")
    return py, root


def build_plist(
    *,
    label: str,
    preset: str,
    python: Path,
    root: Path | None,
    log_path: Path,
    stdio_path: Path,
    path_env: str,
    src: Path | None = None,
) -> str:
    """The plist body. Separate from install_service so it can be checked
    without registering anything with launchd.

    `root` None means an installed copy: no PYTHONPATH, because the package is
    already on the interpreter's path and naming a checkout would let that
    checkout win. The working directory becomes home, since there is no repo
    to sit in and launchd's default of / is not somewhere to write.

    `src` keeps the job alive only while that folder exists. With a bare
    KeepAlive, a moved or deleted source made the watcher exit with
    src_missing and launchd restart it every ten seconds, for months, each
    run appending the same error to both logs. With PathState a missing
    source costs one start and one error line, and launchd starts the
    watcher again by itself when the folder comes back — a remounted volume
    included.
    """
    x = xml_escape  # paths and preset names are user data going into XML
    keep_alive = (
        f"""<dict>
    <key>PathState</key>
    <dict>
      <key>{x(str(src))}</key>
      <true/>
    </dict>
  </dict>"""
        if src is not None
        else "<true/>"
    )
    workdir = root if root is not None else Path.home()
    pythonpath = (
        f"""
    <key>PYTHONPATH</key>
    <string>{x(str(root))}</string>"""
        if root is not None
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{x(label)}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{x(str(python))}</string>
    <string>-m</string>
    <string>folder_watch</string>
    <string>--preset</string>
    <string>{x(preset)}</string>
    <string>--log</string>
    <string>{x(str(log_path))}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{x(str(workdir))}</string>
  <key>EnvironmentVariables</key>
  <dict>{pythonpath}
    <key>PATH</key>
    <string>{x(path_env)}</string>
    <key>HOME</key>
    <string>{x(str(Path.home()))}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  {keep_alive}
  <key>StandardOutPath</key>
  <string>{x(str(stdio_path))}</string>
  <key>StandardErrorPath</key>
  <string>{x(str(stdio_path))}</string>
</dict>
</plist>
"""


def _preset_src(preset: str) -> Path | None:
    """The folder a service's KeepAlive is tied to. A relative src has no
    meaning to launchd, so it gets the unconditional KeepAlive instead."""
    src = load_presets().get(preset, {}).get("src")
    path = Path(src).expanduser() if src else None
    return path if path is not None and path.is_absolute() else None


def install_service(
    *,
    preset: str,
    program: Path,
    log_path: Path | None = None,
) -> Path:
    label = service_label(preset)
    plist = service_plist_path(preset)
    log_path = (log_path or (STATE_DIR / f"{preset}.log")).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    python, root = _resolve_python_and_root(program)
    body = build_plist(
        src=_preset_src(preset),
        label=label,
        preset=preset,
        python=python,
        root=root,
        log_path=log_path,
        stdio_path=stdio_log_path(log_path),
        path_env=launchagent_path_env(),
    )
    plist.write_text(body)
    # Installing means running: clear a pause left by an earlier install.
    _launchctl("enable", _domain_target(preset))
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
    r = subprocess.run(
        ["launchctl", "load", str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            r.stderr.strip() or r.stdout.strip() or "launchctl load failed"
        )
    return plist


# The weekly `prune --ask` job, under the service naming so uninstall_service
# and service_status work on it. The leading underscore keeps it out of the
# way of preset names people actually choose.
PRUNE_JOB = "_weekly-prune"


def install_prune_schedule(*, program: Path) -> Path:
    """A LaunchAgent that runs `prune --ask` on Sundays at 10:00."""
    python, root = _resolve_python_and_root(program)
    env = {"PATH": launchagent_path_env(), "HOME": str(Path.home())}
    if root is not None:
        env["PYTHONPATH"] = str(root)
    out = STATE_DIR / f"{PRUNE_JOB}.out.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist = service_plist_path(PRUNE_JOB)
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": service_label(PRUNE_JOB),
                "ProgramArguments": [str(python), "-m", "folder_watch", "prune", "--ask"],
                "WorkingDirectory": str(root if root is not None else Path.home()),
                "EnvironmentVariables": env,
                "StartCalendarInterval": {"Weekday": 0, "Hour": 10, "Minute": 0},
                "StandardOutPath": str(out),
                "StandardErrorPath": str(out),
            }
        )
    )
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
    r = subprocess.run(
        ["launchctl", "load", str(plist)], capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(
            r.stderr.strip() or r.stdout.strip() or "launchctl load failed"
        )
    return plist


def _domain_target(preset: str) -> str:
    return f"gui/{os.getuid()}/{service_label(preset)}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def is_paused(preset: str) -> bool:
    """Disabled in launchd's own records, which outlive logins and plists."""
    label = f'"{service_label(preset)}"'
    out = _launchctl("print-disabled", f"gui/{os.getuid()}").stdout
    return any(
        label in line and ("=> disabled" in line or "=> true" in line)
        for line in out.splitlines()
    )


def pause_service(*, preset: str) -> None:
    """Stop the service and keep it stopped across logins, plist untouched."""
    if not service_plist_path(preset).is_file():
        raise RuntimeError(f"no service installed for {preset!r}")
    _launchctl("disable", _domain_target(preset))
    _launchctl("bootout", _domain_target(preset))


def resume_service(*, preset: str) -> None:
    plist = service_plist_path(preset)
    if not plist.is_file():
        raise RuntimeError(f"no service installed for {preset!r}")
    _launchctl("enable", _domain_target(preset))
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
    # 37 is "already loaded": resuming a service that was never stopped.
    if r.returncode not in (0, 37):
        raise RuntimeError(r.stderr.strip() or "launchctl bootstrap failed")


def uninstall_service(*, preset: str) -> Path | None:
    plist = service_plist_path(preset)
    if plist.is_file():
        subprocess.run(
            ["launchctl", "unload", str(plist)], capture_output=True, check=False
        )
        plist.unlink(missing_ok=True)
        # A pause outlives the plist; left behind, a later install of the same
        # preset would load disabled and never run.
        _launchctl("enable", _domain_target(preset))
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
    status: dict[str, Any] = {
        "label": label,
        "plist": str(plist),
        "plist_exists": plist.is_file(),
        "loaded": r.returncode == 0,
        "paused": is_paused(preset),
        "launchctl": (r.stdout or r.stderr or "").strip(),
    }
    if not plist.is_file():
        return status
    # The plist copies the preset's src at install time, so editing the
    # preset afterwards leaves launchd keeping alive a folder nobody watches.
    try:
        keep_alive = plistlib.loads(plist.read_bytes()).get("KeepAlive")
    except (OSError, plistlib.InvalidFileException):
        keep_alive = None
    path_state = keep_alive.get("PathState", {}) if isinstance(keep_alive, dict) else {}
    tied_to = next(iter(path_state), None)
    src = _preset_src(preset)
    if tied_to != (str(src) if src is not None else None):
        status["warning"] = (
            f"the plist keeps the job alive for {tied_to or 'any source state'}, "
            f"but the preset's src is {src}; run `folder-watch service install "
            f"--preset {preset}` again"
        )
    return status
