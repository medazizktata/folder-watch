"""macOS LaunchAgent install/uninstall."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from folder_watch.support.paths import LABEL_PREFIX, LAUNCH_AGENTS, STATE_DIR


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
    path_env = f"{local_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>folder_watch</string>
    <string>--preset</string>
    <string>{preset}</string>
    <string>--log</string>
    <string>{log_path}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{root}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{root}</string>
    <key>PATH</key>
    <string>{path_env}</string>
    <key>HOME</key>
    <string>{Path.home()}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
</dict>
</plist>
"""
    plist.write_text(body)
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
    r = subprocess.run(
        ["launchctl", "load", str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "launchctl load failed")
    return plist


def uninstall_service(*, preset: str) -> Path | None:
    plist = service_plist_path(preset)
    if plist.is_file():
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
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
