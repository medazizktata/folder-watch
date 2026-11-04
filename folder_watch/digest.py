"""What each preset did in the last N days, counted from its logs.

Log lines carry their date, so a week is counted by date rather than "since
the last digest". Lines written before dates were added cannot be placed in
time and are skipped.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from folder_watch.presets import load_presets
from folder_watch.support.paths import STATE_DIR

DONE = ("copied", "extracted", "ran")
FAILED = ("fail", "give_up")
_DATED = re.compile(r"^(\d{4}-\d\d-\d\d) \d\d:\d\d:\d\d \w+ (\w+)")


def count_events(paths: list[Path], since: date) -> Counter[str]:
    """Events dated on or after `since`, by kind, across `paths`."""
    counts: Counter[str] = Counter()
    for path in paths:
        try:
            lines = path.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                    day = datetime.fromisoformat(event["ts"]).date()
                    kind = event["event"]
                except (ValueError, KeyError, TypeError):
                    continue
            else:
                m = _DATED.match(line)
                if not m:
                    continue
                day, kind = date.fromisoformat(m.group(1)), m.group(2)
            if day >= since:
                counts[kind] += 1
    return counts


def week(days: int = 7, today: date | None = None) -> dict[str, Counter[str]]:
    """Per preset, the last `days` days (today included) of its default log
    and its rotated copies."""
    since = (today or date.today()) - timedelta(days=days - 1)
    return {
        name: count_events(sorted(STATE_DIR.glob(f"{name}.log*")), since)
        for name in sorted(load_presets())
    }


def summary(per_preset: dict[str, Counter[str]]) -> str | None:
    """One line for a notification, or None when nothing happened."""
    total: Counter[str] = Counter()
    for counts in per_preset.values():
        total.update(counts)
    failed = sum(total[k] for k in FAILED)
    parts = [f"{kind} {total[kind]}" for kind in DONE if total[kind]]
    if failed:
        parts.append(f"{failed} failed")
    if not parts:
        return None
    busiest = sorted(
        (name for name, c in per_preset.items() if any(c[k] for k in DONE + FAILED)),
        key=lambda name: -sum(per_preset[name][k] for k in DONE + FAILED),
    )
    more = f" +{len(busiest) - 3}" if len(busiest) > 3 else ""
    return f"{', '.join(parts)} ({', '.join(busiest[:3])}{more})"
