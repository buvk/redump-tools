"""Shared output replacement, naming and source retention for Xbox/RVZ actions."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .file_transaction import FileTransaction
from .filenames import safe_name
from .game_folders import contains_one_game
from .library_metadata import manifest_for
from .metadata import load_manifest
from .naming_analysis import analyze_manifest_names
from .playlists import playlist_updates
from .source_guard import SourceGuard
from .workflow_layout import plan_layout
from .workflow_manifest import ManifestWorkflow


@dataclass(frozen=True)
class ImageConversionResult:
    status: str
    details: str
    source: Path
    output: Path
    changes: dict[Path, Path]


def convert_image(source: Path, output: Path, manifests: ManifestWorkflow,
                  convert: Callable[[ManifestWorkflow], tuple[str, str]], *, keep_sources: bool = True,
                  force: bool = False, use_redump_names: bool = False, dry_run: bool = False) -> ImageConversionResult:
    if source.is_symlink() or output.is_symlink():
        raise ValueError("Input and output must be regular files, not symbolic links")
    source, output = source.resolve(), output.absolute()
    if source == output:
        raise ValueError("Input and output must be separate regular files; symbolic links are not supported")
    if output.exists() and not force:
        raise FileExistsError(f"Output already exists: {output}; enable Overwrite existing output files to replace it")
    if use_redump_names and manifests.dat_index is None:
        raise ValueError("Use official Redump names requires Redump DAT files")
    if dry_run:
        return ImageConversionResult("Planned", f"Create {output}. Original {'kept' if keep_sources else 'deleted after success'}. "
                                     f"Existing output {'may be overwritten' if force else 'kept'}. "
                                     f"{'Use a unique Redump checksum match for file and folder names.' if use_redump_names else 'Keep existing names.'}",
                                     source, output, {})
    guard = SourceGuard([source])
    changes: dict[Path, Path] = {}
    status, details = "Failed", "Conversion did not complete"
    final_source, final_output = source, output
    with FileTransaction() as transaction:
        playlists = playlist_updates(source.parent, source.parent, {source: output})
        transaction.watch_metadata([source.parent / "manifest.json", *source.parent.glob("*.manifest.json"), *playlists])
        transaction.prepare_output(output)
        managed = ManifestWorkflow(manifests.chdman, manifests.dat_index, manifests.cues, manifests.verbose,
                                   dolphin=manifests.dolphin, on_write=lambda path: transaction.watch_metadata([path]))
        status, details = convert(managed)
        guard.check()
        if status not in {"Completed", "Warning"}:
            raise ValueError(details)
        if use_redump_names:
            metadata = manifest_for(output)
            recorded = load_manifest(metadata) if metadata else None
            if recorded is None or metadata is None:
                raise ValueError("Cannot identify the output manifest for Redump naming")
            analysis = analyze_manifest_names(recorded, manifests.dat_index)
            analysis.require_single_game()
            if not contains_one_game(source.parent, [recorded.title, *analysis.titles], [source, output]):
                raise ValueError("Use official Redump names requires one game per folder; move the game into its own folder first")
            # Replacement applies to the converted image, never other game files.
            disc = next((disc for disc in recorded.disc_entries if output.name in disc.source_files), None)
            if disc is None:
                raise ValueError("Cannot identify the converted disc for Redump naming")
            target = source.parent.with_name(safe_name(next(iter(analysis.titles)))) / analysis.image_name(output, disc.disc)
            if target != output and target.exists():
                if not force or target == source or target.is_symlink():
                    raise FileExistsError(f"Redump output already exists: {target}")
                transaction.prepare_output(target)
            plan = plan_layout(metadata, manifests.dat_index)
            changes = dict(plan.moves)
            final_source = changes.get(source, source)
            final_output = changes.get(output, output)
            plan.apply(transaction, source_guard=guard)
            guard.remap_paths(changes)
            details = details.replace(str(output), str(final_output)) + "\nRedump file and folder names applied."
        guard.check(content=True)
        details = details.replace("Original ISO kept.", "").replace("Source kept.", "").strip()
    guard.check()
    if not keep_sources:
        try:
            final_source.unlink()
        except OSError as error:
            return ImageConversionResult("Warning", f"{details} Could not delete original: {error}", final_source, final_output, changes)
    return ImageConversionResult(status, f"{details}\nOriginal {'kept' if keep_sources else 'deleted after success'}.", final_source, final_output, changes)
