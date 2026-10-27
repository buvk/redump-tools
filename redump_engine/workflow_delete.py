"""Plan and delete an explicit file selection; never remove folders implicitly."""
from __future__ import annotations

from pathlib import Path

from .batch import iter_batch
from .cue import referenced_bin_files
from .events import EventReporter
from .library import LibraryItem
from .systems import system_from_path


def deletion_plan(items: list[LibraryItem], root: Path) -> tuple[list[Path], list[Path]]:
    root = root.resolve()
    selected = {item.path for item in items}
    tracks = {track for item in items if item.kind == "CUE/BIN" for track in item.dependencies
              if track.is_relative_to(item.path.parent) and track.is_file()}
    shared = referenced_bin_files(root, selected) if tracks else set()
    retained = tracks & shared - selected
    files = selected | (tracks - retained)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f"Not an available file within this library: {path}")
    return sorted(files), sorted(retained)


def delete_files(paths: list[Path], events: EventReporter, stop_file: Path | None = None, *, dry_run: bool = False) -> int:
    deleted = failed = 0
    for path in iter_batch(paths, stop_file, False, events):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("File is no longer available; folders and symbolic links are not deleted")
            if dry_run:
                status, detail = "Planned", "Would permanently delete file; no files changed"
            else:
                path.unlink()
                status, detail = "Completed", "File permanently deleted"
            deleted += 1
        except (OSError, ValueError) as exc:
            status, detail = "Failed", str(exc)
            failed += 1
        print(f"[delete] {status.upper()} {path}: {detail}", flush=True)
        events.result(path.name, status, detail, folder=path.parent, source=path, system=system_from_path(path), operation="delete")
    events.summary(processed=deleted, errors=failed)
    return 2 if failed else 0
