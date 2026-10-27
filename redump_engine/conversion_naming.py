"""Plan consistent CHD/file/folder naming before any conversion modifies files."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .chd_ops import ChdmanError
from .filenames import dat_folder_name, dat_output_stem, safe_name
from .game_folder_merge import validate_game_merge
from .game_folders import contains_one_game
from .game_manifests import disc_for_source, game_key, manifest_destination
from .metadata import Manifest, load_manifest, payload_checksums_from_payload_files

if TYPE_CHECKING:
    from .workflow_convert import SourceDisc



@dataclass(frozen=True)
class ConversionPlan:
    folder: Path
    outputs: dict[Path, Path]
    manifest: Path
    retitled_manifest: Manifest | None = None
    merge_folder: bool = False


def conversion_plan(folder: Path, sources: list[SourceDisc], dat_name: str, *,
                    use_redump_names: bool, selected: bool) -> ConversionPlan:
    dedicated = contains_one_game(folder, [dat_name, *(source.source_file.stem for source in sources)],
                                  (source.source_file for source in sources))
    target = folder
    merge_folder = False
    if use_redump_names:
        if not all(source.verified_dat_name for source in sources):
            raise ChdmanError("Use Redump names requires a unique DAT checksum match for every selected disc; turn it off to keep source names")
        if not dedicated:
            raise ChdmanError("Use Redump names requires one game per folder; move the selected game into its own subfolder first")
        names = {dat_folder_name(source.dat_entry.name) for source in sources if source.dat_entry}
        if len(names) != 1:
            raise ChdmanError("Selected discs belong to different Redump games; their folder cannot be renamed together")
        target = folder.with_name(safe_name(next(iter(names))))
        if target != folder:
            if target.exists() or target.is_symlink():
                validate_game_merge(folder, target, dat_name, {source.disc_number for source in sources},
                                    sources[0].dat_entry.system if sources[0].dat_entry else None, [])
                merge_folder = True
            if folder.resolve() in (Path.cwd().resolve(), *Path.cwd().resolve().parents):
                raise ChdmanError("Leave the game folder before using Redump naming; it contains the current working directory")
    outputs = {}
    for source in sources:
        stem = source.source_file.stem
        if use_redump_names and source.dat_entry:
            stem = safe_name(dat_output_stem(source.dat_entry.name, source.disc_number, len(sources)))
        outputs[source.source_file] = folder / f"{stem}.chd"
    if merge_folder:
        validate_game_merge(folder, target, dat_name, {source.disc_number for source in sources},
                            sources[0].dat_entry.system if sources[0].dat_entry else None, list(outputs.values()))
    metadata = folder / "manifest.json" if dedicated or not selected else manifest_destination(sources[0].source_file, dat_name)
    existing = load_manifest(metadata)
    retitled = None
    if existing and game_key(existing.title) != game_key(dat_name):
        covered = set()
        for source in sources:
            disc = disc_for_source(source.source_file, existing)
            if not disc or not source.verified_dat_name or source.dat_entry is None:
                break
            sha1s, crcs = payload_checksums_from_payload_files(disc.payload_files)
            if sha1s != source.dat_entry.payload_sha1_set or crcs != source.dat_entry.payload_crc_set:
                break
            covered.add(disc.disc)
        if covered != {disc.disc for disc in existing.disc_entries}:
            raise ChdmanError(f"Existing manifest belongs to a different title: {metadata}; resolve it before converting")
        retitled = Manifest(existing.schema_version, existing.set_type, dat_folder_name(dat_name), existing.disc_entries, existing.system)
    return ConversionPlan(target, outputs, metadata, retitled, merge_folder)
