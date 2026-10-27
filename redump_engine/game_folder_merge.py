"""Consolidate separately stored discs only into a proven matching game folder."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .source_guard import SourceGuard

from .chd_ops import ChdmanError
from .file_io import write_metadata
from .file_transaction import FileTransaction
from .filenames import game_key
from .game_folders import contains_one_game
from .game_manifests import merge_game_manifest
from .library_metadata import is_manifest
from .metadata import load_manifest


def validate_game_merge(source: Path, target: Path, title: str, discs: set[int],
                        system: str | None, outputs: list[Path]) -> None:
    reason = ""
    if target.is_symlink() or not target.is_dir():
        reason = "it is not a regular game folder"
    elif (target / "manifest.json").is_symlink():
        reason = "its manifest is a symbolic link"
    else:
        manifest = load_manifest(target / "manifest.json")
        if not manifest or not manifest.disc_entries or game_key(manifest.title) != game_key(title):
            reason = "its manifest does not confirm the same game"
        elif system and manifest.system and manifest.system != system:
            reason = "its manifest identifies a different system"
        elif discs & {disc.disc for disc in manifest.disc_entries}:
            reason = "it already records one of these disc numbers"
        elif not contains_one_game(target, [title]):
            reason = "it contains other games"
    if reason:
        raise ChdmanError(f"Cannot combine game folders: destination already exists and {reason}: {target}")
    for path in [*source.iterdir(), *outputs]:
        if path.is_symlink():
            raise ChdmanError(f"Cannot combine game folders containing symbolic links: {path}")
        if is_manifest(path):
            recorded = load_manifest(path) if path.is_file() else None
            if recorded and game_key(recorded.title) != game_key(title):
                raise ChdmanError(f"Cannot combine a folder containing unrelated metadata: {path}")
            continue
        if path.is_dir():
            raise ChdmanError(f"Cannot combine game folders containing subfolders or symbolic links: {path}")
        destination = target / path.name
        if destination.exists() or destination.is_symlink():
            raise ChdmanError(f"Cannot combine game folders; file already exists: {destination}")


def merge_game_folder(source: Path, target: Path, title: str, transaction: FileTransaction, source_guard: SourceGuard | None = None) -> None:
    metadata = [path for path in source.iterdir() if is_manifest(path)]
    incoming = [load_manifest(path) for path in metadata]
    if any(manifest is None or game_key(manifest.title) != game_key(title) for manifest in incoming):
        raise ChdmanError("Cannot combine unreadable or unrelated game manifests")
    target_metadata = target / "manifest.json"
    playlist = target / f"{target.name}.m3u"
    transaction.watch_metadata([*metadata, target_metadata, *target.glob("*.manifest.json"), playlist])
    for manifest in incoming:
        assert manifest is not None
        existing = load_manifest(target_metadata)
        if existing and {disc.disc for disc in manifest.disc_entries} & {disc.disc for disc in existing.disc_entries}:
            raise ChdmanError("Destination already records a disc being combined; no files were overwritten")
        merge_game_manifest(target_metadata, manifest)
    for path in source.iterdir():
        if path not in metadata and not transaction.is_backup(path):
            transaction.transfer(path, target / path.name, source_guard=source_guard)
    for path in metadata:
        path.unlink()
    # Create a missing set playlist without replacing a user's existing playlist.
    combined = load_manifest(target_metadata)
    if combined and len(combined.disc_entries) > 1 and not playlist.exists():
        entries = []
        for disc in sorted(combined.disc_entries, key=lambda entry: entry.disc):
            candidates = sorted((target / name for name in disc.source_files), key=lambda path: path.suffix.lower() != ".chd")
            image = next((path for path in candidates if path.suffix.lower() in {".chd", ".cue", ".iso"} and path.is_file()), None)
            if image is None:
                break
            entries.append(image.name)
        if len(entries) == len(combined.disc_entries):
            write_metadata(playlist, ("\n".join(entries) + "\n").encode("utf-8"))
    if any(source.iterdir()):
        transaction.remove_empty_folder_after_commit(source)
    else:
        transaction.remove_empty_folder(source)
