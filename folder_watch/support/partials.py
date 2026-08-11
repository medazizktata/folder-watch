"""Scratch / incomplete filename detection."""
from __future__ import annotations

PARTIAL_SUFFIXES = (
    ".part",
    ".partial",
    ".tmp",
    ".temp",
    ".crdownload",
    ".download",
    ".aria2",
    ".!ut",
    ".filepart",
)
PARTIAL_PREFIXES = ("~$", ".", "~")


def is_partial_filename(name: str) -> bool:
    """True for in-progress / scratch filenames (AME, browsers, Office, etc.)."""
    base = name.strip()
    if not base or base in (".", ".."):
        return True
    lower = base.lower()
    if lower.endswith(PARTIAL_SUFFIXES):
        return True
    if base.startswith(PARTIAL_PREFIXES):
        return True
    if base.startswith("._"):
        return True
    return False
