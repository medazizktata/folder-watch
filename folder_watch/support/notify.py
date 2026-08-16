"""macOS notifications + Finder reveal."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from folder_watch.support.log import emit

# Where macOS keeps sounds a notification can name. User sounds win, matching
# how Notification Centre itself resolves them.
SOUND_DIRS = (Path.home() / "Library" / "Sounds", Path("/System/Library/Sounds"))
SOUND_SUFFIXES = (".aiff", ".aif", ".wav", ".m4a", ".caf")

# Short and quiet — a click rather than an announcement. This fires once per
# finished file on a folder that can take a batch of them, so anything with a
# tail or a melody stops being information and starts being noise.
DEFAULT_SOUND = "Tink"

# Said once per process. A name that does not resolve means silence, and
# silence is indistinguishable from the feature being off.
_warned_missing: set[str] = set()


def available_sounds() -> list[str]:
    """Every sound name this machine can play, user sounds first."""
    seen: dict[str, None] = {}
    for directory in SOUND_DIRS:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() in SOUND_SUFFIXES:
                seen.setdefault(entry.stem, None)
    return list(seen)


def resolve_sound(name: str | None) -> str | None:
    """The name to hand a notifier, or None if it would be silent anyway.

    Notifiers accept an unknown sound name and simply play nothing, so a typo
    is invisible — the notification still appears, exactly as it does when
    sound is switched off. Checked here so it can be said out loud instead.
    """
    if not name or name.lower() == "none":
        return None
    if any(
        (directory / f"{name}{suffix}").is_file()
        for directory in SOUND_DIRS
        for suffix in SOUND_SUFFIXES
    ):
        return name
    if name not in _warned_missing:
        _warned_missing.add(name)
        emit(
            "warn",
            reason="unknown_sound",
            sound=name,
            note="no such sound; notifying silently",
            known=",".join(available_sounds()[:8]),
        )
    return None


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
    sound: str | None = None,
) -> None:
    """macOS notification. If reveal is set, offers 'Show in folder' (alerter).

    `sound` names a macOS sound to play with it. Every backend below takes one,
    so it rides along with the notification rather than being a second noise
    fired separately — which would drift out of sync with it and would still
    play when the notification itself was suppressed.
    """
    tone = resolve_sound(sound)

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
                        *(["-sound", tone] if tone else []),
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
                        *(["-sound", tone] if tone else []),
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
        if tone:
            script += f' sound name "{esc(tone)}"'
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

