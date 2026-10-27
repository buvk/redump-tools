"""Resolve disc metadata and recorded original sizes without changing files."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .cue import bin_files_from_cue
from .filenames import dat_folder_name, is_xiso, normalize_name
from .game_manifests import disc_for_source
from .metadata import Manifest, load_manifest


class ManifestIndex(Mapping[Path, Manifest | None]):
    """One immutable scan snapshot with constant-time folder lookup."""
    def __init__(self, values: Mapping[Path, Manifest | None]):
        self._values = dict(values)
        self._folders: dict[Path, list[Path]] = {}
        for path in values:
            self._folders.setdefault(path.parent, []).append(path)

    def __getitem__(self, key: Path) -> Manifest | None:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def in_folder(self, folder: Path) -> list[Path]:
        return self._folders.get(folder, [])


def is_manifest(path: Path) -> bool:
    return path.name.lower() == "manifest.json" or path.name.lower().endswith(".manifest.json")


def original_disc_size(path: Path, manifest_path: Path | None,
                       manifests: Mapping[Path, Manifest | None] | None = None) -> int | None:
    if manifest_path is None or is_manifest(path):
        return None
    try:
        manifest = manifests.get(manifest_path) if manifests is not None else load_manifest(manifest_path)
        if manifest is None:
            return None
        disc = disc_for_source(path, manifest)
        if disc is None:
            return None
        payloads = [payload for payload in disc.payload_files if str(payload.get("kind", "")).lower() in {"iso", "bin"}]
        sizes = [payload.get("size") for payload in payloads]
        if not sizes:
            return None
        total = 0
        for size in sizes:
            if type(size) is not int or size < 0:
                return None
            total += size
        return total if total > 0 else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def manifest_for(path: Path, manifests: Mapping[Path, Manifest | None] | None = None) -> Path | None:
    if is_manifest(path):
        return path
    grouped = path.parent / f"{dat_folder_name(path.stem)}.manifest.json"
    if grouped.is_file():
        return grouped
    sidecar = path.with_suffix(".manifest.json")
    if sidecar.is_file():
        return sidecar
    generic = path.parent / "manifest.json"
    nearby = manifests.in_folder(path.parent) if isinstance(manifests, ManifestIndex) else (manifests if manifests is not None else path.parent.glob("*.manifest.json"))
    candidates = [generic, *(candidate for candidate in nearby if candidate.parent == path.parent)]
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            manifest = manifests.get(candidate) if manifests is not None else load_manifest(candidate)
            if manifest:
                names = {str(payload.get("name", "")).casefold() for disc in manifest.disc_entries for payload in disc.payload_files}
                stems = {normalize_name(Path(name).stem) for name in names}
                sources = {name.casefold() for disc in manifest.disc_entries for name in disc.source_files}
                if path.name.casefold() in sources or path.name.casefold() in names or (not is_xiso(path) and (normalize_name(path.stem) in stems or normalize_name(dat_folder_name(path.stem)) == normalize_name(manifest.dat_name))):
                    return candidate
                if path.suffix.lower() == ".cue" and any(track.name.casefold() in names for track in bin_files_from_cue(path)):
                    return candidate
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return None
