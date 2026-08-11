"""macOS notifications + Finder reveal."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024**2:
        return f"{n / 1024:.1f}KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f}MB"
    return f"{n / 1024**3:.1f}GB"


def reveal_in_finder(path: Path) -> None:
    """Open Finder and select/highlight the file (or its parent if missing)."""
    target = path.expanduser()
    try:
        target = target.resolve()
    except OSError:
        pass
    if not target.exists():
        target = target.parent
    # open -R = reveal & select in Finder
    try:
        subprocess.run(["open", "-R", str(target)], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return
    # bring Finder to front
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to activate'],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def notify(
    title: str,
    body: str,
    *,
    reveal: Path | None = None,
) -> None:
    """macOS notification. If reveal is set, offers 'Show in folder' (alerter)."""

    def _run() -> None:
        reveal_path = reveal.expanduser() if reveal is not None else None
        alerter = shutil.which("alerter")
        if alerter and reveal_path is not None:
            try:
                r = subprocess.run(
                    [
                        alerter,
                        "--title",
                        title,
                        "--message",
                        body,
                        "--subtitle",
                        "Show in folder highlights the file",
                        "--actions",
                        "Show in folder",
                        "--close-label",
                        "Close",
                        "--timeout",
                        "45",
                        "--group",
                        "folder-watch-copy",
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
            except (OSError, subprocess.TimeoutExpired):
                return
            raw = (r.stdout or "").strip()
            atype = ""
            avalue = ""
            try:
                payload = json.loads(raw) if raw.startswith("{") else {}
                atype = str(payload.get("activationType") or "")
                avalue = str(payload.get("activationValue") or "")
            except json.JSONDecodeError:
                avalue = raw

            # Button click, or click on the notification body
            want_reveal = (
                avalue == "Show in folder"
                or atype in ("actionClicked", "contentsClicked")
                or (
                    "show" in avalue.lower()
                    and "folder" in avalue.lower()
                )
            )
            if want_reveal:
                reveal_in_finder(reveal_path)
            return

        # Fallback: click notification to reveal (terminal-notifier)
        tn = shutil.which("terminal-notifier")
        if tn and reveal_path is not None:
            # Build a tiny reveal script so quoting survives Drive paths
            script = (
                "#!/bin/bash\n"
                f"open -R {json.dumps(str(reveal_path))}\n"
                "osascript -e 'tell application \"Finder\" to activate'\n"
            )
            try:
                import tempfile as _tf

                with _tf.NamedTemporaryFile(
                    "w", suffix="-fw-reveal.sh", delete=False
                ) as fh:
                    fh.write(script)
                    sh = fh.name
                os.chmod(sh, 0o755)
                subprocess.run(
                    [
                        tn,
                        "-title",
                        title,
                        "-message",
                        body,
                        "-subtitle",
                        "Click to show file in Finder",
                        "-group",
                        "folder-watch-copy",
                        "-execute",
                        sh,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            return

        # Last resort: plain osascript (no button)
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{esc(body)}" with title "{esc(title)}"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    threading.Thread(target=_run, daemon=True, name="fw-notify").start()

