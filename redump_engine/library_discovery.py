"""Read-only library enumeration and construction of disc records."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .cue import bin_files_from_cue
from .filenames import disc_title, game_key, is_xiso, parse_disc_number
from .game_manifests import disc_for_source
from .library_metadata import ManifestIndex, is_manifest, manifest_for, original_disc_size
from .library_models import LibraryItem
from .metadata import Manifest, load_manifest
from .systems import SYSTEM_NAMES, recognize_system, system_from_path


def item_for(path: Path, manifests: Mapping[Path, Manifest | None] | None = None) -> LibraryItem:
    path = path.resolve()
    kind = {".iso": "ISO", ".chd": "CHD", ".rvz": "RVZ", ".cue": "CUE/BIN", ".bin": "BIN (no CUE)"}.get(path.suffix.lower(), "Manifest")
    if is_xiso(path):
        kind = "XISO"
    tracks = tuple(dict.fromkeys(track.resolve() for track in bin_files_from_cue(path))) if kind == "CUE/BIN" else ()
    problem = ""
    if kind == "CUE/BIN":
        missing = [track.name for track in tracks if not track.is_file()]
        if not tracks:
            problem = "CUE has no BIN references"
        elif missing:
            problem = "Missing tracks: " + ", ".join(missing)
        elif any(not track.is_relative_to(path.parent) for track in tracks):
            problem = "Tracks outside the CUE folder are not supported for selected conversion"
    elif kind == "BIN (no CUE)":
        problem = "A CUE file is required to convert this BIN"
    size = path.stat().st_size + sum(track.stat().st_size for track in tracks if track.is_file())
    manifest_path = manifest_for(path, manifests)
    missing_record = manifest_path is None
    existing = None
    disc = None
    if manifest_path is not None:
        try:
            existing = manifests.get(manifest_path) if manifests is not None else load_manifest(manifest_path)
            if kind != "Manifest":
                disc = disc_for_source(path, existing) if existing else None
                missing_record = disc is None or not disc.payload_files
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            missing_record = False
    title = disc_title(path)
    if existing and not missing_record:
        title = existing.title or title
        if disc and existing.disc_count > 1 and parse_disc_number(title) is None:
            title += f" (Disc {disc.disc})"
    system = existing.system if existing and existing.system else "xbox" if kind == "XISO" else system_from_path(path)
    original_id = (system or str(path.parent), game_key(existing.title), disc.disc) if existing and disc else None
    return LibraryItem(path, kind, size, manifest_path, tracks, problem,
                       original_disc_size(path, manifest_path, {manifest_path: existing} if manifest_path else None), missing_record, system, title, original_id)


def scan_library(folder: Path, recursive: bool = True) -> list[LibraryItem]:
    paths: list[Path] = []
    for current, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".") and name.lower() not in {"dats", "cuesheets", "tools", "redump_engine", "tests", "__pycache__", "build", "dist"}]
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                continue
            if path.suffix.lower() in {".iso", ".xiso", ".chd", ".rvz", ".cue", ".bin"} or is_manifest(path):
                paths.append(path)
        if not recursive:
            break
    manifests: dict[Path, Manifest | None] = {}
    for path in paths:
        if is_manifest(path):
            try:
                manifests[path.resolve()] = load_manifest(path)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                manifests[path.resolve()] = None
    manifest_index = ManifestIndex(manifests)
    items = []
    for path in sorted(paths, key=lambda value: str(value).casefold()):
        try:
            items.append(item_for(path, manifest_index))
        except FileNotFoundError:
            # A running batch can rename/remove an item after enumeration.
            continue
    referenced = {track for item in items for track in item.dependencies}
    return [item for item in items if item.path.suffix.lower() != ".bin" or item.path not in referenced]


@dataclass(frozen=True)
class LibrarySnapshot:
    items: list[LibraryItem]
    systems: tuple[str | None, ...]


def scan_library_snapshot(folder: Path, recursive: bool = True) -> LibrarySnapshot:
    items = scan_library(folder, recursive)
    systems = {item.system for item in items}
    root_system = recognize_system(folder.name)
    if root_system:
        systems.add(root_system)
    else:
        # Include empty platform folders, so the selector describes the library itself.
        systems.update(system for child in folder.iterdir() if child.is_dir() and not child.is_symlink()
                       if (system := recognize_system(child.name)))
    return LibrarySnapshot(items, tuple(sorted(systems, key=lambda value: SYSTEM_NAMES.get(value or "", value or "Unknown system"))))
