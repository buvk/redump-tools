from __future__ import annotations

import os
import re
from collections.abc import Iterable
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


def cue_reference_root(folder: Path) -> Path:
    """Use the enclosing system folder, or its immediate collection context."""
    from .systems import recognize_system
    return next((parent for parent in (folder, *folder.parents) if recognize_system(parent.name)), folder.parent)


def referenced_bin_files(root: Path, excluded: Iterable[Path] = ()) -> set[Path]:
    """Read CUE dependencies once; an unreadable subtree must stop source cleanup."""
    ignored = {path.resolve() for path in excluded}
    references = set()

    def unreadable(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in files:
            cue = Path(current) / name
            if cue.suffix.lower() == ".cue" and cue.resolve() not in ignored:
                references.update(track.resolve() for track in bin_files_from_cue(cue))
    return references
