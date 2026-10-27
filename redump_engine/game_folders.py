"""Shared identification of single-game folders and their file aliases."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from .filenames import disc_title, game_key
from .metadata import Manifest, load_manifest
from .systems import recognize_system


def contains_one_game(folder: Path, titles: Iterable[str], sources: Iterable[Path] = (), *,
                      manifests: Mapping[Path, Manifest | None] | None = None) -> bool:
    """Check every disc against known titles or explicit manifest file aliases.

    This checks contents, not whether a loose single image has been organized.
    Source aliases allow a checksum-confirmed DAT title to differ from its input.
    """
    if recognize_system(folder.name):
        return False
    keys = {game_key(title) for title in titles}
    accepted = set(sources)
    for metadata in (folder / "manifest.json", *folder.glob("*.manifest.json")):
        if not metadata.is_file() or metadata.is_symlink():
            continue
        try:
            manifest = manifests[metadata] if manifests is not None and metadata in manifests else load_manifest(metadata)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        if manifest is None or game_key(manifest.title) not in keys:
            continue
        for disc in manifest.disc_entries:
            names = [*disc.source_files, *(str(payload.get("name", "")) for payload in disc.payload_files)]
            accepted.update(folder / name for name in names if name)
    peers = [path for path in folder.iterdir() if path.is_file()
             and path.suffix.lower() in {".iso", ".xiso", ".chd", ".rvz", ".cue"}]
    return all(path in accepted or game_key(disc_title(path)) in keys for path in peers)


def is_game_folder(folder: Path, title: str) -> bool:
    """An existing game folder needs both matching contents and folder context."""
    if not contains_one_game(folder, [title]):
        return False
    if game_key(folder.name) == game_key(title) or recognize_system(folder.parent.name):
        return True
    generic = folder / "manifest.json"
    if generic.is_file() and not generic.is_symlink():
        manifest = load_manifest(generic)
        return manifest is not None and game_key(manifest.title) == game_key(title)
    return False
