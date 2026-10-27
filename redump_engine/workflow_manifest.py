"""Create missing game records from payloads, extracting CHDs only in temporary storage."""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from .chd_ops import Chdman, ChdmanError
from .cuesheets import CueRepository
from .dolphin_ops import DolphinTool
from .file_transaction import FileTransaction
from .filenames import dat_folder_name, parse_disc_number
from .game_manifests import disc_for_source, manifest_destination, merge_game_manifest
from .library import item_for, manifest_for
from .metadata import (
    DatIndex,
    Manifest,
    ManifestDisc,
    load_manifest,
    payload_files_from_paths,
)
from .payload_matching import complete_matches
from .source_guard import SourceGuard
from .systems import system_from_path
from .workflow_extract import ExtractWorkflow


class ManifestWorkflow:
    def __init__(self, chdman: Chdman, dat_index: DatIndex | None, cues: CueRepository, verbose: bool = False,
                 dolphin: DolphinTool | None = None, on_write: Callable[[Path], None] | None = None):
        self.chdman = chdman
        self.dat_index = dat_index
        self.cues = cues
        self.verbose = verbose
        self.dolphin = dolphin
        self.on_write = on_write

    def process_file(self, path: Path) -> tuple[str, str]:
        path = path.resolve()
        item = item_for(path)
        existing_path = manifest_for(path)
        existing = load_manifest(existing_path) if existing_path else None
        disc = disc_for_source(path, existing) if existing else None
        if disc is not None and disc.payload_files:
            return "Skipped", "Manifest record already exists; existing metadata was kept"
        if not item.eligible("manifest"):
            return "Skipped", item.problem or "No missing manifest record for this item"
        if existing and len(existing.disc_entries) > 1 and parse_disc_number(path.stem) is None and disc is None:
            raise ValueError("Cannot identify this disc in a multi-disc manifest; add its disc number to the filename")
        guard = SourceGuard(list(dict.fromkeys([path, *item.dependencies])))
        with FileTransaction() as transaction:
            transaction.watch_metadata([path.parent / "manifest.json", *path.parent.glob("*.manifest.json")])

            def watch(destination: Path) -> None:
                transaction.watch_metadata([destination])
                if self.on_write:
                    self.on_write(destination)

            managed = ManifestWorkflow(self.chdman, self.dat_index, self.cues, self.verbose,
                                       dolphin=self.dolphin, on_write=watch)
            result = managed._create(path, item.kind, list(item.dependencies), existing)
            guard.check(content=True)
        return result

    def _create(self, path: Path, kind: str, dependencies: list[Path], existing: Manifest | None) -> tuple[str, str]:
        if kind == "CHD":
            return self._from_chd(path, existing)
        if kind == "RVZ":
            if self.dolphin is None:
                raise ValueError("DolphinTool is required to restore the ISO for this RVZ manifest")
            with tempfile.TemporaryDirectory(prefix="redump_manifest_") as tmp:
                original = Path(tmp) / path.with_suffix(".iso").name
                self.dolphin.convert(path, original, rvz=False)
                return self.record_iso(path, original, existing)
        kind = "iso" if kind == "ISO" else "bin"
        payloads = [path] if kind == "iso" else dependencies
        return self._save(path, payloads, kind, existing)

    def record_iso(self, source: Path, original: Path, existing: Manifest | None = None) -> tuple[str, str]:
        return self._save(source, [original], "iso", existing)

    def _from_chd(self, path: Path, existing: Manifest | None) -> tuple[str, str]:
        return self.inspect_chd_payloads(path, lambda originals, kind: self._save(path, originals, kind, existing), existing)

    def inspect_chd_payloads(self, path: Path, consume: Callable[[list[Path], str], tuple[str, str]],
                             existing: Manifest | None = None) -> tuple[str, str]:
        """Extract into disposable storage; the consumer never needs to publish files."""
        info = self.chdman.info(path)
        if info.logical_size is not None and shutil.disk_usage(tempfile.gettempdir()).free < info.logical_size:
            raise ChdmanError("Not enough free space in the temporary folder to extract this CHD")
        workflow = ExtractWorkflow(self.chdman, self.dat_index, self.cues, verbose=self.verbose,
                                   keep_sources=True, rename_folder=False)
        system, _, entry = workflow._source_context(path, existing)
        is_cd = info.has_cd_metadata and not info.has_dvd_metadata
        entry = workflow._resolve_ambiguous_cd_name_match(path.name, entry, is_cd, system)
        decision, is_cd = workflow._info_decision(info, system, entry)
        if decision == "unknown":
            decision = "bin-cue" if is_cd else "iso"
        with tempfile.TemporaryDirectory(prefix="redump_manifest_") as tmp:
            print(f"[chd] Temporarily extracting {path.name}; source CHD will be kept", flush=True)
            payloads = workflow._extract_by_decision(path, Path(tmp), decision, entry, is_cd,
                                                    track_count_hint=max(info.track_count, 1))
            workflow._validate_extracted_files(payloads)
            kind = "iso" if decision == "iso" else "bin"
            originals = [payload for payload in payloads if payload.suffix.lower() == f".{kind}"]
            return consume(originals, kind)

    def _save(self, source: Path, originals: list[Path], kind: str, existing: Manifest | None) -> tuple[str, str]:
        if not originals or any(not path.is_file() for path in originals):
            raise ChdmanError("Cannot create a manifest without all expected payload files")
        payloads = payload_files_from_paths(originals, kind, base=None if source.suffix.lower() in {".chd", ".rvz"} else source.parent)
        system = (existing.system if existing else None) or system_from_path(source)
        matches = complete_matches(self.dat_index, payloads, system) if self.dat_index else []
        entry = matches[0] if len(matches) == 1 else None
        title = existing.title if existing else entry.name if entry else source.stem
        existing_disc = disc_for_source(source, existing) if existing else None
        number = parse_disc_number(source.stem) or (parse_disc_number(entry.name) if entry else None) or (existing_disc.disc if existing_disc else 1)
        # Temporary output names can differ from Redump's original track names.
        if entry and source.suffix.lower() in {".chd", ".rvz"}:
            roms = [rom for rom in entry.roms if Path(rom.name).suffix.lower() == f".{kind}"]
            for payload in payloads:
                exact = [rom for rom in roms if rom.sha1 and rom.sha1.lower() == str(payload.get("sha1", "")).lower()]
                if len(exact) == 1:
                    payload["name"] = exact[0].name
        sources: list[str] = list(existing_disc.source_files) if existing_disc else []
        if source.name not in sources:
            sources.append(source.name)
        system = system or (entry.system if entry else None)
        manifest = Manifest(1, "iso" if kind == "iso" else "bin-cue", dat_folder_name(title), [ManifestDisc(number, payloads, sources)], system)
        destination = manifest_destination(source, title)
        # A canonical manifest may already contain this disc under a DAT-resolved name.
        if destination.is_file():
            canonical = load_manifest(destination)
            recorded = canonical.disc_entry_for(number) if canonical else None
            if recorded and recorded.payload_files:
                return "Skipped", "Manifest record already exists; existing metadata was kept"
        if self.on_write:
            self.on_write(destination)
        merge_game_manifest(destination, manifest)
        match = "payload matches Redump DAT" if entry else "payload checksums recorded; no unique DAT match"
        return "Completed", f"Created manifest: {destination} ({match}). Source files kept."
