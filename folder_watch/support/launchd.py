"""macOS LaunchAgent install/uninstall."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

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
) -> str:
    """The plist body. Separate from install_service so it can be checked
    without registering anything with launchd.

    `root` None means an installed copy: no PYTHONPATH, because the package is
    already on the interpreter's path and naming a checkout would let that
    checkout win. The working directory becomes home, since there is no repo
    to sit in and launchd's default of / is not somewhere to write.
    """
    x = xml_escape  # paths and preset names are user data going into XML
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
  <true/>
  <key>StandardOutPath</key>
  <string>{x(str(stdio_path))}</string>
  <key>StandardErrorPath</key>
  <string>{x(str(stdio_path))}</string>
</dict>
</plist>
"""


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
        label=label,
        preset=preset,
        python=python,
        root=root,
        log_path=log_path,
        stdio_path=stdio_log_path(log_path),
        path_env=launchagent_path_env(),
    )
    plist.write_text(body)
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


def uninstall_service(*, preset: str) -> Path | None:
    plist = service_plist_path(preset)
    if plist.is_file():
        subprocess.run(
            ["launchctl", "unload", str(plist)], capture_output=True, check=False
        )
        plist.unlink(missing_ok=True)
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
    return {
        "label": label,
        "plist": str(plist),
        "plist_exists": plist.is_file(),
        "loaded": r.returncode == 0,
        "launchctl": (r.stdout or r.stderr or "").strip(),
    }
