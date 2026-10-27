"""Preview and repair a checksum-identified game's names without recompressing it."""
from __future__ import annotations

import copy
import json
import os
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cue import bin_files_from_cue, cue_reference_root, referenced_bin_files
from .file_io import write_metadata
from .file_transaction import FileTransaction
from .filenames import safe_name
from .game_folders import contains_one_game
from .game_manifests import disc_for_source
from .library_metadata import is_manifest, manifest_for
from .metadata import DatIndex, Manifest, manifest_from_dict
from .naming_analysis import NamingAnalysis, analyze_manifest_names, payload_filename
from .playlists import playlist_updates
from .source_guard import SourceGuard


def _signature(disc: dict[str, Any]) -> list[tuple[str, ...]]:
    return sorted(tuple(str(payload.get(key, "")).casefold() for key in ("kind", "size", "sha1", "crc32"))
                  for payload in disc["payload_files"])


def _associated_images(manifests: dict[Path, Manifest]) -> dict[tuple[Path, int], list[Path]]:
    associated: dict[tuple[Path, int], list[Path]] = {}
    for folder in dict.fromkeys(path.parent for path in manifests):
        for image in folder.iterdir():
            if image.suffix.lower() not in {".chd", ".rvz", ".iso", ".xiso", ".cue"} or not image.is_file():
                continue
            owner = manifest_for(image, manifests)
            disc = disc_for_source(image, manifests[owner]) if owner in manifests else None
            if owner is not None and disc:
                associated.setdefault((owner, disc.disc), []).append(image)
    return associated


def cue_reference_update(old: Path, new: Path, moves: dict[Path, Path]) -> bytes | None:
    original = old.read_bytes()
    text = original.decode("utf-8-sig")
    for line in text.splitlines():
        if re.match(r"\s*(?:FILE|DATAFILE)\s+", line, re.IGNORECASE) and not re.match(r'\s*(?:FILE|DATAFILE)\s+"[^\"]+"', line, re.IGNORECASE):
            raise ValueError(f"Unsupported unquoted CUE reference: {old}")

    def track_reference(match: re.Match[str]) -> str:
        track = (old.parent / match[2]).resolve()
        renamed = moves.get(track, track)
        name = os.path.relpath(renamed, new.parent).replace("\\", "/")
        return match[1] + name + match[3]

    updated = re.sub(r'(?im)^(\s*(?:FILE|DATAFILE)\s+")([^\"]+)("[^\r\n]*)', track_reference, text)
    return updated.encode("utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8") if updated != text else None


@dataclass
class LayoutPlan:
    source_folder: Path
    folder: Path
    manifest: Path
    moves: dict[Path, Path]
    writes: dict[Path, bytes]
    obsolete: list[Path]
    fingerprints: dict[Path, tuple[int, int] | None]
    directories: dict[Path, tuple[str, ...] | None]
    title: str
    copies: set[Path] = field(default_factory=set)

    @property
    def changed(self) -> bool:
        return bool(self.moves or self.obsolete or any(not path.exists() or path.read_bytes() != data for path, data in self.writes.items()))

    def description(self, *, full_paths: bool = False) -> str:
        base = Path(os.path.commonpath([self.source_folder, self.folder]))

        def display(path: Path) -> str:
            return str(path.relative_to(base)) if not full_paths and path.is_relative_to(base) else str(path)

        lines = [self.title]
        for old, new in self.moves.items():
            action = "Copy shared file" if old in self.copies else "Rename file" if old.parent == new.parent else "Move file into game folder"
            lines.append(f"{action}:\n  {display(old)} → {display(new)}")
        if self.obsolete:
            lines.append("Combine manifests:\n" + "\n".join(
                f"  Merge {display(path)} into {display(self.manifest)}, then remove the old manifest." for path in self.obsolete))
        for path in self.writes:
            if path == self.manifest and self.obsolete:
                continue
            lines.append(f"Update {'manifest' if path == self.manifest else 'file references'}:\n  {display(path)}")
        return "\n\n".join(lines) if self.changed else self.title + "\nNames and folders are already correct."

    def apply(self, transaction: FileTransaction | None = None, *, source_guard: SourceGuard | None = None) -> None:
        for folder, names in self.directories.items():
            current = tuple(sorted(file.name for file in folder.iterdir())) if folder.exists() else None
            if folder.is_symlink() or current != names:
                raise ValueError(f"Folder changed since planning; refresh and retry: {folder}")
        for path, fingerprint in self.fingerprints.items():
            stat = path.stat() if path.exists() else None
            current = (stat.st_size, stat.st_mtime_ns) if stat else None
            if path.is_symlink() or current != fingerprint:
                raise ValueError(f"File changed since planning; refresh and retry: {path}")
        for new in self.moves.values():
            if new.exists() or new.is_symlink():
                raise FileExistsError(f"Destination appeared after preview: {new}")
        with (nullcontext(transaction) if transaction is not None else FileTransaction()) as transaction:
            transaction.watch_metadata([*self.writes, *self.obsolete,
                                        *(path for path in self.moves if path.suffix.lower() in {".cue", ".m3u"})])
            transaction.mkdir(self.folder)
            for old, new in self.moves.items():
                for parent in reversed([new.parent, *new.parent.parents]):
                    if parent.is_relative_to(self.folder):
                        transaction.mkdir(parent)
                transaction.transfer(old, new, copy=old in self.copies, source_guard=source_guard)
            for path, data in self.writes.items():
                write_metadata(path, data)
            for path in self.obsolete:
                path.unlink()
            if self.source_folder != self.folder:
                if not any(self.source_folder.iterdir()):
                    transaction.remove_empty_folder(self.source_folder)
                else:
                    transaction.remove_empty_folder_after_commit(self.source_folder)


def plan_layout(path: Path, index: DatIndex | None) -> LayoutPlan:
    if path.is_symlink():
        raise ValueError("Cannot repair a symbolic-link manifest")
    path = path.resolve()
    if not is_manifest(path):
        path = manifest_for(path) or path
    if index is None:
        raise ValueError("Redump DATs are required to fix naming/layout")
    if path.is_symlink():
        raise ValueError("Cannot repair a symbolic-link manifest")
    if not path.is_file():
        raise ValueError("Create a manifest before fixing naming/layout")
    selected_raw = json.loads(path.read_text(encoding="utf-8"))
    selected = manifest_from_dict(selected_raw, path)
    analysis = analyze_manifest_names(selected, index)
    title = analysis.require_single_game()
    folder = path.parent
    target = folder.with_name(safe_name(title))
    if folder != target and folder.resolve() in (Path.cwd().resolve(), *Path.cwd().resolve().parents):
        raise ValueError("Leave the game folder before fixing its layout; it contains the current working directory")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"Destination is not a regular folder: {target}")
    manifests: dict[Path, tuple[Manifest, NamingAnalysis, dict[str, Any]]] = {}
    for current in dict.fromkeys([folder, target] if target.exists() else [folder]):
        metadata = [file for file in current.iterdir() if is_manifest(file)]
        if not metadata:
            raise ValueError(f"Existing destination has no matching game manifest: {current}")
        for file in metadata:
            if file.is_symlink():
                raise ValueError(f"Symbolic-link metadata is not supported: {file}")
            raw = selected_raw if file == path else json.loads(file.read_text(encoding="utf-8"))
            manifest = selected if file == path else manifest_from_dict(raw, file)
            matched = analyze_manifest_names(manifest, index)
            if matched.require_single_game() != title:
                raise ValueError(f"Folder contains unrelated game metadata: {file}; organize its games first")
            manifests[file] = manifest, matched, raw
        titles = [value[0].title for file, value in manifests.items() if file.parent == current]
        if not contains_one_game(current, [title, *titles], manifests={file: value[0] for file, value in manifests.items()}):
            raise ValueError(f"This is a shared/system folder; move the game into its own subfolder first: {current}")
    systems = {manifest.system for manifest, _, _ in manifests.values() if manifest.system}
    systems.update(entry.system for _, matched, _ in manifests.values() for entry in matched.entries.values() if entry.system)
    if len(systems) > 1:
        raise ValueError("Manifests/DATs identify different systems; resolve the conflict before fixing layout")
    moves: dict[Path, Path] = {}
    owners: dict[Path, Path] = {}
    by_disc: dict[int, dict[str, Any]] = {}
    snapshots: set[Path] = set(manifests)
    root_data = copy.deepcopy(next(iter(manifests.values()))[2])
    images = _associated_images({file: value[0] for file, value in manifests.items()})

    def add_move(old: Path, new: Path) -> None:
        if not old.is_file() or old.is_symlink():
            raise ValueError(f"File is unavailable or a symbolic link: {old}")
        old = old.resolve()
        snapshots.add(old)
        if old == new:
            return
        if new.exists() or new.is_symlink():
            raise FileExistsError(f"Naming/layout would overwrite an existing file: {new}")
        if new in owners and owners[new] != old:
            raise FileExistsError(f"Two files would receive the same name: {new}")
        if old in moves and moves[old] != new:
            raise ValueError(f"Shared track requires conflicting names: {old}")
        owners[new] = old
        moves[old] = new

    for metadata, (manifest, matched, raw) in manifests.items():
        for key, value in raw.items():
            if key not in {"title", "discs", "system"}:
                if key in root_data and root_data[key] != value:
                    raise ValueError(f"Conflicting manifest field: {key}")
                root_data[key] = copy.deepcopy(value)
        for disc in manifest.disc_entries:
            entry = matched.entries[disc.disc]
            record = copy.deepcopy(next(raw_disc for raw_disc in raw["discs"] if int(raw_disc["disc"]) == disc.disc))
            for payload in record["payload_files"]:
                original = metadata.parent / str(payload["name"])
                if original.exists():
                    if not original.resolve().is_relative_to(metadata.parent):
                        raise ValueError(f"Payload escapes the game folder: {original}")
                    add_move(original, target / payload_filename(payload, entry))
                payload["name"] = payload_filename(payload, entry)
            record.setdefault("source_files", [])
            for image in images.get((metadata, disc.disc), []):
                destination = target / matched.image_name(image, disc.disc)
                add_move(image, destination)
                record["source_files"] = [destination.name if name == image.name else name for name in record["source_files"]]
                if destination.name not in record["source_files"]:
                    record["source_files"].append(destination.name)
                if image.suffix.lower() == ".cue":
                    for track in bin_files_from_cue(image):
                        if not track.resolve().is_relative_to(metadata.parent):
                            raise ValueError(f"CUE track escapes the game folder: {track}")
                        payloads = [payload for payload in disc.payload_files if (metadata.parent / str(payload.get("name", ""))).resolve() == track.resolve()]
                        if len(payloads) != 1:
                            raise ValueError(f"Cannot identify track in manifest: {track}")
                        add_move(track, target / payload_filename(payloads[0], entry))
            if disc.disc in by_disc:
                existing = by_disc[disc.disc]
                if _signature(existing) != _signature(record):
                    raise ValueError(f"Conflicting payload records for disc {disc.disc}")
                if sorted(existing["payload_files"], key=lambda value: value["name"]) != sorted(record["payload_files"], key=lambda value: value["name"]):
                    raise ValueError(f"Conflicting payload metadata for disc {disc.disc}")
                for key, value in record.items():
                    if key not in {"payload_files", "source_files"} and key in existing and existing[key] != value:
                        raise ValueError(f"Conflicting disc metadata field: {key}")
                    existing.setdefault(key, value)
                existing["source_files"] = list(dict.fromkeys([*existing["source_files"], *record["source_files"]]))
            else:
                by_disc[disc.disc] = record
    moving_tracks = {old.resolve() for old in moves if old.suffix.lower() == ".bin"}
    if moving_tracks:
        selected_cues = {path for path in snapshots if path.suffix.lower() == ".cue"}
        shared_tracks = referenced_bin_files(cue_reference_root(folder), selected_cues)
        if moving_tracks & shared_tracks:
            raise ValueError("Cannot rename or move tracks referenced by other CUE files; resolve the shared references before fixing names and folders")
    for current in dict.fromkeys([folder, target] if target.exists() else [folder]):
        for image in current.iterdir():
            if image.suffix.lower() in {".chd", ".rvz", ".iso", ".xiso", ".cue", ".bin"} and image not in snapshots:
                raise ValueError(f"Image/track lacks an unambiguous manifest association: {image}")
        for local in current.glob("*.m3u"):
            add_move(local, target / local.name)
    writes: dict[Path, bytes] = {}
    for old, new in [*moves.items(), *((file, file) for file in snapshots if file.suffix.lower() == ".cue" and file not in moves)]:
        if old.suffix.lower() != ".cue":
            continue
        updated = cue_reference_update(old, new, moves)
        if updated is not None:
            writes[new] = updated
    for current in dict.fromkeys([folder, target] if target.exists() else [folder]):
        for playlist, data in playlist_updates(current, current, moves, relocate_folder=False).items():
            snapshots.add(playlist)
            writes[moves.get(playlist, playlist)] = data
    root_data["title"] = title
    root_data["discs"] = [by_disc[number] for number in sorted(by_disc)]
    if systems:
        root_data["system"] = next(iter(systems))
    canonical = target / "manifest.json"
    writes[canonical] = (json.dumps(root_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    obsolete = [file for file in manifests if file != canonical]
    fingerprints = {file: (file.stat().st_size, file.stat().st_mtime_ns) if file.exists() else None for file in snapshots | set(writes)}
    directories = {current: tuple(sorted(file.name for file in current.iterdir())) if current.exists() else None
                   for current in {folder, target}}
    return LayoutPlan(folder, target, canonical, moves, writes, obsolete, fingerprints, directories, title)
