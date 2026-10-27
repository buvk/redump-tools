"""Convert an explicitly selected Xbox disc ISO while preserving its original record."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .actions import ACTIONS
from .filenames import is_xiso
from .game_manifests import disc_for_source, merge_game_manifest
from .library import item_for, manifest_for
from .metadata import load_manifest, payload_files_from_paths
from .workflow_manifest import ManifestWorkflow


def run_xiso_tool(tool: Path, arguments: list[str]) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([str(tool), *arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", creationflags=flags)
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f"extract-xiso failed (exit {result.returncode})")


def convert_xiso(source: Path, tool: Path, manifests: ManifestWorkflow, *,
                 dry_run: bool = False, keep_source: bool = True) -> tuple[str, str]:
    if source.suffix.lower() != ".iso" or is_xiso(source):
        return "Skipped", "Select an original Xbox ISO, not an existing XISO"
    if not ACTIONS["xiso"].supports("ISO", item_for(source).system):
        raise ValueError("This image belongs to a different console; Xbox conversion is unavailable")
    destination = source.with_name(source.stem + ".xiso.iso")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Output already exists: {destination}")
    if dry_run:
        return "Planned", f"Record original ISO checksums, then rewrite Xbox image to {destination}. " + (
            "Original ISO kept." if keep_source else "Original ISO deleted; it cannot be reconstructed from the XISO.")
    if not tool.is_file():
        raise FileNotFoundError(f"extract-xiso is missing: {tool}")
    if shutil.disk_usage(source.parent).free < source.stat().st_size * 3:
        raise OSError("Not enough free space for the working copy, rewritten XISO, and final output")
    before = source.stat()
    previous = manifest_for(source)
    if previous:
        recorded = load_manifest(previous)
        disc = disc_for_source(source, recorded) if recorded else None
        if disc and disc.payload_files:
            actual = payload_files_from_paths([source], "iso")[0]
            if len(disc.payload_files) != 1 or any(str(actual.get(key, "")).lower() != str(disc.payload_files[0].get(key, "")).lower()
                                                    for key in ("size", "sha1", "crc32")):
                raise ValueError("Original ISO differs from its manifest; resolve the mismatch before conversion")
    manifests.process_file(source)
    metadata_path = manifest_for(source)
    manifest = load_manifest(metadata_path) if metadata_path else None
    disc = disc_for_source(source, manifest) if manifest else None
    if manifest is None or disc is None or not disc.payload_files or metadata_path is None:
        raise ValueError("An original ISO manifest is required before Xbox conversion")
    # Rewrite only a private copy: extract-xiso may rename or modify its input.
    with tempfile.TemporaryDirectory(prefix=".redump-xiso-", dir=source.parent) as tmp:
        workspace = Path(tmp)
        working = workspace / destination.name
        output = workspace / "output"
        output.mkdir()
        shutil.copy2(source, working)
        run_xiso_tool(tool, ["-r", "-d", str(output), str(working)])
        rewritten = output / destination.name
        if not rewritten.exists() and working.is_file():
            # v2.7.1 leaves already-optimized images untouched and exits successfully.
            # The listing below must still accept that image before it is published.
            rewritten = working
        if not rewritten.is_file() or rewritten.stat().st_size == 0:
            raise RuntimeError("extract-xiso did not produce the expected image")
        run_xiso_tool(tool, ["-l", str(rewritten)])
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Original ISO changed during conversion; no output was published")
        # Exclusive creation refuses collisions even if another process creates the output meanwhile.
        with rewritten.open("rb") as incoming, destination.open("xb") as outgoing:
            try:
                shutil.copyfileobj(incoming, outgoing)
            except Exception:
                outgoing.close()
                destination.unlink()
                raise
        try:
            manifest.system = "xbox"
            if destination.name not in disc.source_files:
                disc.source_files.append(destination.name)
            merge_game_manifest(metadata_path, manifest)
        except Exception:
            destination.unlink()
            raise
    if not keep_source:
        source.unlink()
    return "Completed", (f"Created {destination}. Original ISO {'kept' if keep_source else 'deleted'}. "
                         "XISO files listed successfully; original manifest checksums do not verify the XISO. "
                         "The original ISO cannot be reconstructed from this output.")
