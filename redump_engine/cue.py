from __future__ import annotations

import re
from pathlib import Path

_QUOTED = re.compile(r'"([^"]+)"')


def bin_files_from_cue(cue_path: Path) -> list[Path]:
    """Return the .bin files referenced by FILE/DATAFILE lines of a cue, in order."""
    bins: list[Path] = []
    if not cue_path.exists():
        return bins
    for line in cue_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if not (lower.startswith("file") or lower.startswith("datafile")):
            continue
        match = _QUOTED.search(stripped)
        if not match:
            continue
        candidate = cue_path.parent / match.group(1)
        if candidate.suffix.lower() == ".bin":
            bins.append(candidate)
    return bins


def count_tracks_in_cue(cue_path: Path) -> int:
    count = 0
    for line in cue_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().lower().startswith("track "):
            count += 1
    return max(count, 1)
