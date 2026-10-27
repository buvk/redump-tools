"""Place individual discs and their metadata in game folders without overwriting files."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .cue import cue_reference_root, referenced_bin_files
from .filenames import dat_folder_name, safe_name
from .game_folders import contains_one_game
from .game_manifests import disc_for_source
from .library import item_for, organize_destination, scan_library
from .metadata import DatIndex, Manifest, load_manifest
from .naming_analysis import analyze_manifest_names, payload_filename
from .organization_group import combine_game_plans
from .playlists import playlist_updates
from .workflow_layout import LayoutPlan, cue_reference_update, plan_layout


def _manifest_bytes(manifest: Manifest, sources: list[Path]) -> bytes:
    """Keep extension fields when splitting or combining disc records."""
    raw = {}
    discs = {}
    for source in sources:
        if not source.exists():
            continue
        value = json.loads(source.read_text(encoding="utf-8-sig"))
        for key, content in value.items():
            if key not in {"title", "discs", "system", "schema_version", "set_type"} and key in raw and raw[key] != content:
                raise ValueError(f"Conflicting manifest field: {key}")
            raw.setdefault(key, copy.deepcopy(content))
        for disc in value.get("discs", []):
            discs.setdefault(int(disc["disc"]), copy.deepcopy(disc))
    updates: dict[str, Any] = manifest.to_dict()
    for disc in updates["discs"]:
        previous = discs.get(int(disc["disc"]), {})
        previous.update(disc)
        disc.update(previous)
    raw.update(updates)
    return (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def plan_folder_organization(path: Path, index: DatIndex | None = None) -> LayoutPlan:
    if path.is_symlink():
        raise ValueError("Cannot organize a symbolic-link image")
    path = path.resolve()
    item = item_for(path)
    if item.kind not in {"CHD", "RVZ", "ISO", "XISO", "CUE/BIN"} or item.problem:
        raise ValueError(item.problem or "Not a supported disc image")
    destination = organize_destination(path)
    if destination == path.parent:
        return LayoutPlan(path.parent, path.parent, item.manifest or path.parent / "manifest.json", {}, {}, [], {}, {}, path.parent.name)
    source_manifest = item.manifest
    metadata: dict[Path, bytes | None] = {}
    target_names: dict[Path, str] = {}
    original_title: str | None = None
    if source_manifest:
        if source_manifest.is_symlink():
            raise ValueError("Cannot organize symbolic-link metadata")
        manifest = load_manifest(source_manifest)
        assert manifest is not None
        disc = disc_for_source(path, manifest)
        if disc is None:
            raise ValueError("Cannot identify this disc in its manifest")
        original_title = manifest.title
        disc = copy.deepcopy(disc)
        analysis = analyze_manifest_names(manifest, index)
        entry = analysis.entries.get(disc.disc)
        if entry is not None:
            if entry.system and manifest.system and entry.system != manifest.system:
                raise ValueError("The manifest and matched DAT identify different systems")
            manifest.system = manifest.system or entry.system
            canonical_title = dat_folder_name(entry.name)
            destination = path.parent / safe_name(canonical_title)
            manifest.title = canonical_title
            target_names[path] = analysis.image_name(path, disc.disc)
            for payload in disc.payload_files:
                original = path.parent / str(payload.get('name', ''))
                renamed = payload_filename(payload, entry)
                if original in item.dependencies:
                    target_names[original] = renamed
                payload['name'] = renamed
            disc.source_files = [target_names[path]]
        target_manifest = destination / "manifest.json"
        if target_manifest.is_symlink():
            raise ValueError(f"Destination manifest is a symbolic link: {target_manifest}")
        existing = load_manifest(target_manifest)
        if destination.exists() and existing is None and not contains_one_game(destination, [manifest.title, original_title]):
            raise ValueError(f"Destination contains another game: {destination}")
        if existing and existing.title not in {manifest.title, original_title}:
            raise ValueError(f"An unrelated manifest already exists: {target_manifest}")
        entries = {entry.disc: entry for entry in existing.disc_entries} if existing else {}
        if disc.disc in entries and entries[disc.disc] != disc:
            previous = entries[disc.disc]
            if previous.payload_files != disc.payload_files:
                raise ValueError(f"Cannot combine disc {disc.disc}: the original disc records differ in {target_manifest}")
            # Alternate images (for example an ISO and its XISO) may share the
            # same original payload record while listing different source files.
            disc.source_files = list(dict.fromkeys([*previous.source_files, *disc.source_files]))
        entries[disc.disc] = disc
        if existing and existing.system and manifest.system and existing.system != manifest.system:
            raise ValueError(f"Conflicting systems in destination manifest: {target_manifest}")
        combined = Manifest(manifest.schema_version, manifest.set_type, manifest.title, list(entries.values()), manifest.system or (existing.system if existing else None))
        metadata[target_manifest] = _manifest_bytes(combined, [target_manifest, source_manifest])
        remaining = [other for other in scan_library(path.parent, recursive=False)
                     if other.path != path and other.kind != "Manifest" and other.manifest == source_manifest]
        remaining_discs = [disc_for_source(other.path, manifest) for other in remaining]
        if not remaining:
            metadata[source_manifest] = None
        elif all(entry is not None for entry in remaining_discs):
            numbers = {entry.disc for entry in remaining_discs if entry is not None}
            retained = Manifest(manifest.schema_version, manifest.set_type, original_title or manifest.title,
                                [entry for entry in manifest.disc_entries if entry.disc in numbers], manifest.system)
            metadata[source_manifest] = _manifest_bytes(retained, [source_manifest])

    transfers = [(source, destination / target_names.get(source, str(source.relative_to(path.parent)))) for source in (path, *item.dependencies)]
    if destination.exists() and not contains_one_game(destination, [destination.name]):
        raise ValueError(f"Destination contains another game: {destination}")
    if len({target for _, target in transfers}) != len(transfers):
        raise ValueError("Two source files would receive the same name")
    for _, target in transfers:
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Destination already exists: {target}")
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("A destination track escapes the game folder")
    # Shared tracks must stay available to other CUE files.
    shared = referenced_bin_files(cue_reference_root(path.parent), [path]) if item.dependencies else set()
    moves = dict(transfers)
    metadata.update(playlist_updates(path.parent, path.parent, moves))
    if path.suffix.lower() == '.cue':
        updated = cue_reference_update(path, moves[path], moves)
        if updated is not None:
            metadata[moves[path]] = updated
    writes = {target: content for target, content in metadata.items() if content is not None}
    obsolete = [target for target, content in metadata.items() if content is None]
    snapshots = set(moves) | set(moves.values()) | set(metadata)
    if source_manifest:
        snapshots.add(source_manifest)
    fingerprints = {file: (file.stat().st_size, file.stat().st_mtime_ns) if file.exists() else None for file in snapshots}
    directories = {folder: tuple(sorted(file.name for file in folder.iterdir())) if folder.exists() else None
                   for folder in {path.parent, destination}}
    copies = {source for source, _ in transfers if source.resolve() in shared}
    return LayoutPlan(path.parent, destination, destination / 'manifest.json', moves, writes, obsolete,
                      fingerprints, directories, destination.name, copies)


def plan_organization(path: Path, index: DatIndex | None) -> LayoutPlan:
    """Choose one safe plan for folder organization or checksum-based naming."""
    if path.is_symlink():
        raise ValueError("Cannot organize a symbolic-link image or manifest")
    path = path.resolve()
    metadata = path if path.name.lower() == 'manifest.json' or path.name.lower().endswith('.manifest.json') else item_for(path).manifest
    if metadata is not None and index is not None:
        manifest = load_manifest(metadata)
        if manifest is not None:
            analysis = analyze_manifest_names(manifest, index)
            if analysis.entries and not analysis.unresolved:
                # Dedicated game folders can be renamed/merged as a unit. Loose
                # images in shared folders are planned individually instead.
                image = path if metadata != path else next((item.path for item in scan_library(metadata.parent, recursive=False)
                                                           if item.kind != 'Manifest' and item.manifest == metadata), None)
                if image is not None and organize_destination(image) == metadata.parent:
                    return plan_layout(metadata, index)
    if metadata is not None:
        manifest = load_manifest(metadata)
        selected_disc = disc_for_source(path, manifest) if manifest and path != metadata else None
        analysis = analyze_manifest_names(manifest, index) if manifest else None
        selected_entry = analysis.entries.get(selected_disc.disc) if analysis and selected_disc else None
        selected_title = dat_folder_name(selected_entry.name) if selected_entry else None
        associated = [item.path for item in scan_library(metadata.parent, recursive=False)
                      if item.kind != "Manifest" and item.manifest == metadata]
        images = []
        numbers = set()
        for image in associated:
            disc = disc_for_source(image, manifest) if manifest else None
            entry = analysis.entries.get(disc.disc) if analysis and disc else None
            same_game = bool(selected_title and entry and dat_folder_name(entry.name) == selected_title)
            same_disc = bool(selected_disc and disc and disc.disc == selected_disc.disc)
            all_one_game = bool(path == metadata and analysis and not analysis.unresolved and len(analysis.titles) == 1)
            if same_game or same_disc or all_one_game:
                images.append(image)
                if disc:
                    numbers.add(disc.disc)
        if len(images) > 1:
            return combine_game_plans([plan_folder_organization(image, index) for image in images], metadata, numbers)
        if metadata == path:
            if len(associated) == 1:
                return plan_folder_organization(associated[0], index)
            raise ValueError("Select a game image to identify which game in this manifest should be organized")
    return plan_folder_organization(path, index)


def organize_file(path: Path, dry_run: bool = False) -> tuple[str, str]:
    """Compatibility entry point for simple folder moves."""
    item = item_for(path)
    if item.kind not in {'CHD', 'RVZ', 'ISO', 'XISO', 'CUE/BIN'} or item.problem:
        return 'Skipped', item.problem or 'Not a supported disc image'
    plan = plan_folder_organization(path)
    if not plan.changed:
        return 'Skipped', 'Already in its game folder'
    if dry_run:
        return 'Planned', plan.description()
    plan.apply()
    return 'Completed', f'Moved into {plan.folder}'
