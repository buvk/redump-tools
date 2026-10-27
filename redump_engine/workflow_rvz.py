"""Lossless ISO/RVZ conversion with original payload records and rollback."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .dolphin_ops import DolphinTool
from .file_io import write_metadata
from .file_transaction import FileTransaction
from .game_manifests import disc_for_source, merge_game_manifest
from .library import item_for, manifest_for
from .metadata import ManifestDisc, load_manifest, payload_files_from_paths
from .playlists import playlist_updates
from .workflow_manifest import ManifestWorkflow


def check_payload(path: Path, disc: ManifestDisc) -> None:
    actual = payload_files_from_paths([path], "iso")[0]
    if len(disc.payload_files) != 1:
        raise ValueError("An RVZ disc must have exactly one original ISO payload record")
    recorded = disc.payload_files[0]
    if not recorded.get("sha1") or any(str(actual.get(key, "")).lower() != str(recorded.get(key, "")).lower()
                                       for key in ("size", "sha1", "crc32")):
        raise ValueError("ISO payload does not match its manifest; source kept and changes rolled back")


def process_rvz(source: Path, tool: DolphinTool, manifests: ManifestWorkflow, *, extract: bool = False,
                dry_run: bool = False, keep_source: bool = True) -> tuple[str, str]:
    source = source.resolve()
    action = "rvz-extract" if extract else "rvz"
    if not item_for(source).eligible(action):
        return "Skipped", "Select an RVZ to extract or a GameCube/Wii ISO to compress"
    destination = source.with_suffix(".iso" if extract else ".rvz")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Output already exists: {destination}. RVZ actions never overwrite images.")
    if dry_run:
        return "Planned", (f"{'Restore ISO' if extract else 'Compress losslessly to RVZ and verify an ISO round trip'}: {destination}. "
                           f"Source {'kept' if keep_source else 'deleted only after verification'}; file and folder names kept.")
    if not tool.executable.is_file():
        raise FileNotFoundError(f"DolphinTool is missing: {tool.executable}")
    before = source.stat()
    playlists = playlist_updates(source.parent, source.parent, {source: destination})
    metadata = {source.parent / "manifest.json", *source.parent.glob("*.manifest.json"), *playlists}
    with tempfile.TemporaryDirectory(prefix=".redump-rvz-", dir=source.parent) as tmp:
        workspace = Path(tmp)
        working = workspace / destination.name
        with FileTransaction() as transaction:
            transaction.watch_metadata(metadata)
            manifests = ManifestWorkflow(manifests.chdman, manifests.dat_index, manifests.cues, manifests.verbose,
                                         dolphin=tool, on_write=lambda path: transaction.watch_metadata([path]))
            if not extract:
                metadata_path = manifest_for(source)
                recorded = load_manifest(metadata_path) if metadata_path else None
                disc = disc_for_source(source, recorded) if recorded else None
                if disc and disc.payload_files:
                    check_payload(source, disc)
                manifests.process_file(source)
            tool.convert(source, working, rvz=not extract)
            if extract:
                original = working
            else:
                original = workspace / source.name
                tool.convert(working, original, rvz=False)
            metadata_path = manifest_for(source)
            recorded = load_manifest(metadata_path) if metadata_path else None
            disc = disc_for_source(source, recorded) if recorded else None
            if disc and disc.payload_files:
                check_payload(original, disc)
            elif extract:
                # Hash restored ISO bytes, never the compressed RVZ container.
                manifests.record_iso(source, original, recorded)
                metadata_path = manifest_for(source)
                recorded = load_manifest(metadata_path) if metadata_path else None
                disc = disc_for_source(source, recorded) if recorded else None
            if not recorded or disc is None or metadata_path is None:
                raise ValueError("Could not record the original ISO manifest")
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Source changed during conversion; no output published")
            if destination.name not in disc.source_files:
                disc.source_files.append(destination.name)
            merge_game_manifest(metadata_path, recorded)
            transaction.transfer(working, destination)
            for path, content in playlists.items():
                write_metadata(path, content)
    if not keep_source:
        try:
            source.unlink()
        except OSError as exc:
            return "Warning", f"Created and verified {destination}, but could not remove source: {exc}"
    return "Completed", (f"Created {destination}. " + ("Restored ISO checked against manifest." if extract else
                          "ISO round trip matches original manifest checksums; no scrubbing.") +
                          f" Source {'kept' if keep_source else 'deleted'}.")
