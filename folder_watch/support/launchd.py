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


def service_label(preset: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in preset)
    return f"{LABEL_PREFIX}.{safe}"


def service_plist_path(preset: str) -> Path:
    return LAUNCH_AGENTS / f"{service_label(preset)}.plist"


def _resolve_python_and_root(program: Path) -> tuple[Path, Path]:
    """Prefer venv python + package root over a bash wrapper (Desktop TCC)."""
    program = program.expanduser().resolve()
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
    root: Path,
    log_path: Path,
    stdio_path: Path,
    path_env: str,
) -> str:
    """The plist body. Separate from install_service so it can be checked
    without registering anything with launchd."""
    x = xml_escape  # paths and preset names are user data going into XML
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
  <string>{x(str(root))}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{x(str(root))}</string>
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
    local_bin = Path.home() / ".local" / "bin"
    body = build_plist(
        label=label,
        preset=preset,
        python=python,
        root=root,
        log_path=log_path,
        stdio_path=stdio_log_path(log_path),
        path_env=f"{local_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
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
