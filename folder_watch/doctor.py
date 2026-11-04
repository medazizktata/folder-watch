"""`folder-watch doctor`: check what a watcher needs and cannot check itself.

Each check answers a question that otherwise went unasked until something had
been failing for weeks: a service launchd restarts forever after a crash that
happens before folder_watch is even imported, a notifier that is not installed,
a destination that is full, and a source macOS will not let a background job
read. That last one is checked by asking launchd itself, with the interpreter
the services run, because the answer depends on who is asking.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from folder_watch.presets import load_presets
from folder_watch.support.launchd import (
    LAUNCH_AGENTS,
    _resolve_python_and_root,
    launchagent_path_env,
    service_status,
)
from folder_watch.support.paths import LABEL_PREFIX, STATE_DIR

# Folders macOS keeps from background jobs until the job's binary is allowed.
PROTECTED = ("Desktop", "Documents", "Downloads")
LOW_SPACE_BYTES = 2 * 1024**3
PROBE_TIMEOUT = 15.0

Finding = tuple[str, str]  # (level, message); level is "ok", "warn" or "error"


def check_notifiers() -> list[Finding]:
    found = []
    for tool, role in (
        ("alerter", "notifications with buttons (Watch new path, Clean up)"),
        ("terminal-notifier", "plain notifications"),
    ):
        path = shutil.which(tool, path=launchagent_path_env())
        found.append(
            ("ok", f"{tool}: {path}")
            if path
            else ("warn", f"{tool} not found where services look; no {role}")
        )
    return found


def _launchctl_print(label: str) -> dict[str, str]:
    r = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True, text=True, check=False,
    )
    info: dict[str, str] = {}
    if r.returncode != 0:
        return info
    for line in r.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in ("state", "last exit code", "runs") and key not in info:
            info[key] = value
    return info


def check_services() -> list[Finding]:
    """Every folder-watch LaunchAgent: loaded, its last exit, and whether its
    preset still exists. A watcher that crashes before folder_watch loads (a
    bad interpreter, a missing module) leaves nothing in its log; launchd's
    exit code is the only trace."""
    presets = load_presets()
    found: list[Finding] = []
    for plist in sorted(LAUNCH_AGENTS.glob(f"{LABEL_PREFIX}.*.plist")):
        label = plist.name[: -len(".plist")]
        name = label[len(LABEL_PREFIX) + 1 :]
        info = _launchctl_print(label)
        if not info:
            found.append(("error", f"service {name}: plist installed but not loaded"))
            continue
        code = info.get("last exit code", "")
        if code not in ("0", "(never exited)", ""):
            found.append(
                ("error", f"service {name}: last exit {code} after {info.get('runs')} "
                 f"run(s); see its .out log")
            )
        if name.startswith("_"):
            if code in ("0", "(never exited)", ""):
                found.append(("ok", f"job {name}: {info.get('state', 'loaded')}"))
            continue
        if name not in presets:
            found.append(("error", f"service {name}: its preset no longer exists"))
            continue
        warning = service_status(preset=name).get("warning")
        if warning:
            found.append(("warn", f"service {name}: {warning}"))
        elif code in ("0", "(never exited)", ""):
            found.append(("ok", f"service {name}: {info.get('state', 'loaded')}"))
    return found


def check_destinations() -> list[Finding]:
    found: list[Finding] = []
    for name, cfg in sorted(load_presets().items()):
        raw = cfg.get("to")
        if not raw:
            continue
        dest = Path(str(raw)).expanduser()
        if not dest.is_dir():
            found.append(("warn", f"{name}: destination {dest} does not exist"))
            continue
        if not os.access(dest, os.W_OK):
            found.append(("error", f"{name}: destination {dest} is not writable"))
            continue
        free = shutil.disk_usage(dest).free
        if free < LOW_SPACE_BYTES:
            found.append(
                ("warn", f"{name}: {free / 1024**3:.1f} GB free at {dest}")
            )
        else:
            gb = free / 1024**3
            found.append(("ok", f"{name}: destination writable, {gb:.0f} GB free"))
    return found


def protected_category(path: Path) -> str | None:
    try:
        rel = path.expanduser().relative_to(Path.home())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts and rel.parts[0] in PROTECTED else None


def probe(python: Path, folder: Path, timeout: float = PROBE_TIMEOUT) -> str:
    """List `folder` from a one-shot launchd job running `python`. Returns
    "ok", the error it hit (e.g. "Operation not permitted") or "no answer"."""
    label = f"{LABEL_PREFIX}._doctor-probe"
    work = STATE_DIR / "_doctor-probe"
    work.mkdir(parents=True, exist_ok=True)
    result = work / "result"
    result.unlink(missing_ok=True)
    code = (
        "import os, sys\n"
        "try:\n"
        "    os.listdir(sys.argv[1]); msg = 'ok'\n"
        "except OSError as e:\n"
        "    msg = e.strerror or str(e)\n"
        "open(sys.argv[2], 'w').write(msg)\n"
    )
    plist = work / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(python), "-c", code, str(folder), str(result)],
        "RunAtLoad": True,
        "WorkingDirectory": str(work),
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                   capture_output=True, check=False)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if result.is_file() and result.read_text():
                return result.read_text()
            time.sleep(0.2)
        return "no answer"
    finally:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                       capture_output=True, check=False)


def check_protected_folders(program: Path) -> list[Finding]:
    """One launchd probe per protected folder that a preset uses: macOS
    grants access per folder category and per binary, not per path."""
    python, _root = _resolve_python_and_root(program)
    wanted: dict[str, Path] = {}
    for cfg in load_presets().values():
        for key in ("src", "to"):
            raw = cfg.get(key)
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            category = protected_category(path)
            if category and path.is_dir():
                wanted.setdefault(category, path)
    found: list[Finding] = []
    for category, path in sorted(wanted.items()):
        answer = probe(python, path)
        found.append(
            ("ok", f"launchd can read ~/{category} ({python.name})")
            if answer == "ok"
            else ("error", f"launchd cannot read ~/{category}: {answer}. Services using "
                  f"it will fail; move the folder out of ~/{category} or give "
                  f"{python} Full Disk Access")
        )
    return found
