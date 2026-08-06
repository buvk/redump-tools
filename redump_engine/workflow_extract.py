from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
import re

from .chd_ops import Chdman, ChdmanError
from .cuesheets import CueRepository
from .filenames import dat_folder_name, dat_output_stem, normalize_name, parse_disc_number, rename_if_needed
from .metadata import (
    DatEntry,
    DatIndex,
    Manifest,
    ManifestDisc,
    PayloadFile,
    compute_sha1,
    describe_dat_match_ambiguity,
    has_unresolved_name_ambiguity,
    load_manifest,
    match_dat_entries,
    match_dat_entries_for_files,
    payload_files_from_paths,
    save_manifest,
    select_dat_entry,
)

CD_THRESHOLD_BYTES = 800 * 1024 * 1024


@dataclass
class ExtractResult:
    game_dir: Path
    extracted_files: list[Path]
    removed_files: list[Path]
    strategy: str
    decision: str
    dat_name: str


class ExtractWorkflow:
    def __init__(
        self,
        chdman: Chdman,
        dat_index: DatIndex | None,
        cues: CueRepository,
        dry_run: bool = False,
        verbose: bool = False,
    ):
        self.chdman = chdman
        self.dat_index = dat_index
        self.cues = cues
        self.dry_run = dry_run
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[verbose][extract] {message}")

    def process_game_dir(self, game_dir: Path) -> list[ExtractResult]:
        self._log(f"processing game directory: {game_dir}")
        chd_files = sorted(game_dir.glob("*.chd"))
        self._log(f"found chd files: {[p.name for p in chd_files]}")
        results: list[ExtractResult] = []
        for chd_file in chd_files:
            results.append(self._extract_one(game_dir, chd_file))

        if not self.dry_run and results:
            self._finalize_m3u_after_extract(game_dir, results)

        if not self.dry_run and results:
            desired = dat_folder_name(results[0].dat_name)
            target = game_dir.parent / desired
            if desired and desired != game_dir.name and not target.exists():
                self._log(f"renaming folder: {game_dir.name} -> {desired}")
                game_dir.rename(target)
                for item in results:
                    item.game_dir = target
        return results

    def _extract_one(self, game_dir: Path, chd_file: Path) -> ExtractResult:
        manifest = load_manifest(game_dir / "manifest.json")
        name_dat_matches = match_dat_entries(chd_file.name, self.dat_index)
        dat_entry = name_dat_matches[0] if name_dat_matches else None

        if manifest:
            disc_number = parse_disc_number(chd_file.stem) or 1
            manifest_disc = manifest.disc_entry_for(disc_number)
            manifest_tracks = manifest_disc.track_count if manifest_disc else 1
            is_cd_style = manifest.set_type == "bin-cue" or manifest_tracks > 1
            self._log(
                "input="
                f"{chd_file.name}, manifest=yes, "
                f"name_dat_match={dat_entry.name if dat_entry else 'none'}, "
                f"manifest_type={manifest.set_type}, manifest_tracks={manifest_tracks}, "
                f"is_cd_style={is_cd_style}"
            )
            decision = self._manifest_decision(manifest.set_type, manifest_tracks)
            extracted_files = self._extract_by_decision(
                chd_file,
                game_dir,
                decision,
                dat_entry,
                is_cd_style=is_cd_style,
                track_count_hint=max(manifest_tracks, 1),
            )
            strategy = "manifest"
        else:
            info = self.chdman.info(chd_file)
            is_cd_style = info.track_count > 0 and not (
                info.logical_size is not None and info.logical_size > CD_THRESHOLD_BYTES
            )
            dat_entry = self._resolve_ambiguous_cd_name_match(chd_file.name, dat_entry, is_cd_style)
            self._log(
                "input="
                f"{chd_file.name}, manifest=no, "
                f"name_dat_match={dat_entry.name if dat_entry else 'none'}, "
                f"logical_size={info.logical_size}, track_count={info.track_count}, "
                f"is_cd_style={is_cd_style}"
            )
            decision = self._heuristic_decision(info.logical_size, info.track_count, dat_entry)
            self._log_heuristic_override(info.logical_size, info.track_count, dat_entry, decision)
            if decision == "unknown":
                extracted_files, decision = self._bruteforce_extract(
                    chd_file,
                    game_dir,
                    dat_entry,
                    is_cd_style=is_cd_style,
                )
                strategy = "bruteforce"
            else:
                extracted_files = self._extract_by_decision(
                    chd_file,
                    game_dir,
                    decision,
                    dat_entry,
                    is_cd_style=is_cd_style,
                    track_count_hint=max(info.track_count, dat_entry.track_count if dat_entry else 1, 1),
                )
                strategy = "heuristic"

        removed_files = self._cleanup_after_extract(game_dir, chd_file)
        checksum_dat_matches: list[DatEntry] = []
        if not self.dry_run:
            checksum_dat_matches = match_dat_entries_for_files(extracted_files, self.dat_index)
            checksum_dat_entry = checksum_dat_matches[0] if checksum_dat_matches else None
            self._log(
                "checksum dat match="
                f"{checksum_dat_entry.name if checksum_dat_entry else 'none'}"
            )
        else:
            checksum_dat_entry = None

        resolved_dat_entry = self._resolve_dat_entry_for_decision(
            name_dat_matches,
            checksum_dat_matches,
            decision,
            chd_file.name,
        )
        if dat_entry and checksum_dat_entry and dat_entry is not resolved_dat_entry:
            self._log(
                "name/checksum DAT mismatch; selected alternate DAT entry: "
                f"name='{dat_entry.name}'({dat_entry.expected_format}) "
                f"checksum='{checksum_dat_entry.name}'({checksum_dat_entry.expected_format}) "
                f"decision={decision}"
            )
        manifest_dat_name = manifest.dat_name if manifest and manifest.dat_name else game_dir.name
        dat_name = resolved_dat_entry.name if resolved_dat_entry else manifest_dat_name
        dat_name = dat_folder_name(dat_name)
        self._log(f"resolved dat_name={dat_name}, decision={decision}, strategy={strategy}")
        extracted_files = self._rename_and_replace_cue(game_dir, extracted_files, resolved_dat_entry, decision)
        self._write_extract_manifest(game_dir, chd_file, extracted_files, decision, dat_name)

        return ExtractResult(
            game_dir=game_dir,
            extracted_files=extracted_files,
            removed_files=removed_files,
            strategy=strategy,
            decision=decision,
            dat_name=dat_name,
        )

    def _write_extract_manifest(
        self,
        game_dir: Path,
        source_chd: Path,
        extracted: list[Path],
        decision: str,
        dat_name: str,
    ) -> None:
        if self.dry_run:
            return

        payload_file = self._primary_payload_file(extracted)
        self._log(
            f"writing manifest for {source_chd.name}: decision={decision}, dat_name={dat_name}, "
            f"payload={payload_file.name if payload_file else 'none'}"
        )

        disc_number = parse_disc_number(source_chd.stem) or 1
        payload_files = self._payload_files_from_extracted(extracted)
        new_disc = ManifestDisc(
            disc=disc_number,
            payload_files=payload_files,
        )

        existing = load_manifest(game_dir / "manifest.json")
        if existing:
            merged = [d for d in existing.disc_entries if d.disc != disc_number]
            merged.append(new_disc)
            merged.sort(key=lambda d: d.disc)

            manifest = Manifest(
                schema_version=1,
                set_type="iso" if decision == "iso" else "bin-cue",
                title=dat_name or existing.dat_name,
                disc_entries=merged,
            )
        else:
            manifest = Manifest(
                schema_version=1,
                set_type="iso" if decision == "iso" else "bin-cue",
                title=dat_name,
                disc_entries=[new_disc],
            )
        save_manifest(game_dir / "manifest.json", manifest)

    @staticmethod
    def _manifest_decision(file_type: str, tracks: int) -> str:
        if file_type == "iso":
            return "iso"
        if file_type == "bin-cue" and tracks > 1:
            return "bin-cue-split"
        if file_type == "bin-cue":
            return "bin-cue"
        return "unknown"

    @staticmethod
    def _heuristic_decision(logical_size: int | None, track_count: int, dat_entry: DatEntry | None) -> str:
        if track_count > 1:
            return "bin-cue-split"
        if logical_size and logical_size > CD_THRESHOLD_BYTES:
            return "iso"
        if dat_entry and dat_entry.expected_format == "iso":
            return "iso"
        if dat_entry and dat_entry.expected_format == "bin-cue":
            return "bin-cue-split" if dat_entry.track_count > 1 else "bin-cue"
        return "unknown"

    def _log_heuristic_override(
        self,
        logical_size: int | None,
        track_count: int,
        dat_entry: DatEntry | None,
        decision: str,
    ) -> None:
        if not self.verbose or decision != "iso":
            return
        if logical_size is None or logical_size <= CD_THRESHOLD_BYTES:
            return
        if track_count > 1:
            return
        if dat_entry is None or dat_entry.expected_format == "iso":
            return

        self._log(
            "large logical size overrides DAT format; choosing iso "
            f"(logical_size={logical_size}, dat_format={dat_entry.expected_format})"
        )

    def _resolve_ambiguous_cd_name_match(
        self,
        candidate_name: str,
        dat_entry: DatEntry | None,
        is_cd_style: bool,
    ) -> DatEntry | None:
        if (
            dat_entry is None
            or not is_cd_style
            or self.dat_index is None
            or dat_entry.expected_format != "iso"
        ):
            return dat_entry

        key = normalize_name(Path(candidate_name).stem)
        candidate_disc = parse_disc_number(Path(candidate_name).stem)
        matches = [entry for entry in self.dat_index.entries if normalize_name(entry.name) == key]
        if len(matches) < 2:
            return dat_entry

        bin_cue_matches = [entry for entry in matches if entry.expected_format == "bin-cue"]
        if candidate_disc is not None:
            disc_specific = [
                entry for entry in bin_cue_matches if parse_disc_number(entry.name) == candidate_disc
            ]
            if disc_specific:
                chosen = disc_specific[0]
                self._log(
                    "ambiguous CD DAT name match; preferring bin-cue variant: "
                    f"{chosen.name}"
                )
                return chosen

        if bin_cue_matches:
            chosen = bin_cue_matches[0]
            self._log(
                "ambiguous CD DAT name match; preferring bin-cue variant: "
                f"{chosen.name}"
            )
            return chosen

        return dat_entry

    def _extract_by_decision(
        self,
        chd_file: Path,
        game_dir: Path,
        decision: str,
        dat_entry: DatEntry | None,
        is_cd_style: bool,
        track_count_hint: int = 1,
    ) -> list[Path]:
        stem = dat_output_stem(dat_entry.name if dat_entry else chd_file.stem, parse_disc_number(chd_file.stem), 2)

        if self.dry_run:
            if decision == "iso":
                return [game_dir / f"{stem}.iso"]
            if decision == "bin-cue-split":
                track_total = max(track_count_hint, 2)
                bins = [game_dir / f"{stem} (Track {idx:02d}).bin" for idx in range(1, track_total + 1)]
                return [game_dir / f"{stem}.cue", *bins]
            return [game_dir / f"{stem}.cue", game_dir / f"{stem}.bin"]

        if decision == "iso":
            output_iso = game_dir / f"{stem}.iso"
            info = self.chdman.info(chd_file)
            if info.has_cd_metadata and not info.has_dvd_metadata:
                self._log(f"method=extractcd -ob (iso), output={output_iso.name}")
                self.chdman.extractcd_to_iso(chd_file, output_iso)
            else:
                self._log(f"method=extractdvd (iso), output={output_iso.name}")
                self.chdman.extractdvd(chd_file, output_iso)
            return [output_iso]

        output_cue = game_dir / f"{stem}.cue"
        if decision == "bin-cue-split":
            try:
                self._log(f"method=splitbin, output={output_cue.name}")
                self.chdman.splitbin(chd_file, output_cue)
            except ChdmanError:
                self._log("splitbin unavailable; falling back to extractcd for bin/cue")
                self.chdman.extractcd(chd_file, output_cue)
        elif decision == "bin-cue":
            self._log(f"method=extractcd (bin-cue), output={output_cue.name}")
            self.chdman.extractcd(chd_file, output_cue)
        else:
            raise RuntimeError(f"Unknown extraction decision: {decision}")

        extracted = [output_cue]
        extracted.extend(self._bin_files_from_cue(output_cue))
        return extracted

    def _bruteforce_extract(
        self,
        chd_file: Path,
        game_dir: Path,
        dat_entry: DatEntry | None,
        is_cd_style: bool,
    ) -> tuple[list[Path], str]:
        if self.dry_run:
            fallback = "bin-cue"
            if dat_entry and dat_entry.expected_format == "iso":
                fallback = "iso"
            return self._extract_by_decision(chd_file, game_dir, fallback, dat_entry, is_cd_style=is_cd_style), fallback

        with tempfile.TemporaryDirectory(prefix="redump_extract_") as tmp:
            temp_dir = Path(tmp)
            cd_probe_looked_like_cue = False

            if is_cd_style:
                cue_path = temp_dir / "trial.cue"
                self.chdman.extractcd(chd_file, cue_path)
                bin_candidates = self._bin_files_from_cue(cue_path)
                if dat_entry and dat_entry.sha1_set:
                    for candidate in bin_candidates:
                        if compute_sha1(candidate).lower() in dat_entry.sha1_set:
                            return self._extract_by_decision(
                                chd_file,
                                game_dir,
                                "bin-cue",
                                dat_entry,
                                is_cd_style=is_cd_style,
                            ), "bin-cue"

            iso_path = temp_dir / "trial.iso"
            try:
                if is_cd_style:
                    self.chdman.extractcd(chd_file, iso_path)
                    if self._looks_like_cue_descriptor(iso_path):
                        cd_probe_looked_like_cue = True
                        bin_file = self._find_first_bin_from_cue(iso_path)
                        if bin_file is None:
                            sibling_bin = iso_path.with_suffix(".bin")
                            bin_file = sibling_bin if sibling_bin.exists() else None
                        if bin_file is not None:
                            iso_path.unlink(missing_ok=True)
                            bin_file.rename(iso_path)
                else:
                    self.chdman.extractdvd(chd_file, iso_path)
            except ChdmanError:
                pass
            else:
                if dat_entry and dat_entry.sha1_set:
                    if compute_sha1(iso_path).lower() in dat_entry.sha1_set:
                        return self._extract_by_decision(
                            chd_file,
                            game_dir,
                            "iso",
                            dat_entry,
                            is_cd_style=is_cd_style,
                        ), "iso"

                # If the CD probe produced a cue-style descriptor, this CHD behaves like
                # bin/cue payload and should not be forced down the iso extraction path.
                if is_cd_style and cd_probe_looked_like_cue:
                    return self._extract_by_decision(
                        chd_file,
                        game_dir,
                        "bin-cue",
                        dat_entry,
                        is_cd_style=is_cd_style,
                    ), "bin-cue"

                # No match, but ISO extraction completed, so use it as fallback.
                return self._extract_by_decision(
                    chd_file,
                    game_dir,
                    "iso",
                    dat_entry,
                    is_cd_style=is_cd_style,
                ), "iso"

        return self._extract_by_decision(
            chd_file,
            game_dir,
            "bin-cue",
            dat_entry,
            is_cd_style=is_cd_style,
        ), "bin-cue"

    def _cleanup_after_extract(self, game_dir: Path, chd_file: Path) -> list[Path]:
        removed: list[Path] = []
        for path in [chd_file]:
            if self.dry_run:
                removed.append(path)
                continue
            if path.exists():
                self._log(f"cleanup remove: {path.name}")
                path.unlink()
                removed.append(path)
        return removed

    def _rename_and_replace_cue(
        self,
        game_dir: Path,
        extracted: list[Path],
        dat_entry: DatEntry | None,
        decision: str,
    ) -> list[Path]:
        dat_name = dat_entry.name if dat_entry else game_dir.name
        extracted = self._apply_dat_names(extracted, dat_entry)
        self._replace_cue_with_trusted_source(dat_name, extracted, decision, dat_entry)
        return extracted if self.dry_run else self._existing_extracted_files(extracted)

    def _apply_dat_names(self, extracted: list[Path], dat_entry: DatEntry | None) -> list[Path]:
        if not dat_entry:
            return extracted

        self._log(f"enforcing DAT names for {dat_entry.name}")
        renamed = self._enforce_dat_names(extracted, dat_entry)
        self._verify_dat_names(renamed, dat_entry)
        return renamed

    def _replace_cue_with_trusted_source(
        self,
        dat_name: str,
        extracted: list[Path],
        decision: str,
        dat_entry: DatEntry | None,
    ) -> None:
        if self.dry_run or decision == "iso":
            return

        cue = self._first_file_with_suffix(extracted, ".cue")
        if cue is None:
            return

        if cue.exists():
            self._log(f"replacing cue with trusted source: {cue.name}")
            cue.unlink()

        copied = self.cues.copy_trusted_cue(
            dat_name,
            cue,
            expected_bin_name=self._expected_bin_name(extracted),
        )
        if not copied and dat_entry is not None:
            raise RuntimeError(f"Trusted cue not found for DAT entry: {dat_name}")
        if copied:
            self._log(f"trusted cue copied: {cue.name}")

    @staticmethod
    def _existing_extracted_files(extracted: list[Path]) -> list[Path]:
        return [path for path in extracted if path.exists()]

    @staticmethod
    def _first_file_with_suffix(extracted: list[Path], suffix: str) -> Path | None:
        for path in extracted:
            if path.suffix.lower() == suffix:
                return path
        return None

    @staticmethod
    def _expected_bin_name(extracted: list[Path]) -> str | None:
        bin_files = [path for path in extracted if path.suffix.lower() == ".bin"]
        if len(bin_files) == 1:
            return bin_files[0].name
        return None

    @staticmethod
    def _decision_expected_format(decision: str) -> str | None:
        if decision == "iso":
            return "iso"
        if decision in {"bin-cue", "bin-cue-split"}:
            return "bin-cue"
        return None

    def _resolve_dat_entry_for_decision(
        self,
        name_dat_matches: list[DatEntry],
        checksum_dat_matches: list[DatEntry],
        decision: str,
        source_name: str,
    ) -> DatEntry | None:
        if has_unresolved_name_ambiguity(name_dat_matches, checksum_dat_matches):
            self._log(describe_dat_match_ambiguity(source_name, name_dat_matches, checksum_dat_matches))

        return select_dat_entry(
            name_dat_matches,
            checksum_dat_matches,
            desired_format=self._decision_expected_format(decision),
        )

    def _finalize_m3u_after_extract(self, game_dir: Path, results: list[ExtractResult]) -> None:
        existing_m3u = sorted(game_dir.glob("*.m3u"))
        multi_disc = len(results) > 1
        decisions = {r.decision for r in results}
        bin_cue_decisions = {"bin-cue", "bin-cue-split"}

        # Only keep playlists for multi-disc uniform targets.
        if not multi_disc:
            for path in existing_m3u:
                self._log(f"cleanup remove: {path.name}")
                path.unlink(missing_ok=True)
            return

        entries: list[Path] = []
        if decisions and decisions.issubset(bin_cue_decisions):
            entries = self._playlist_entries_for_suffix(results, ".cue")
        elif decisions == {"iso"}:
            entries = self._playlist_entries_for_suffix(results, ".iso")

        if not entries:
            for path in existing_m3u:
                self._log(f"cleanup remove: {path.name}")
                path.unlink(missing_ok=True)
            return

        dat_name = dat_folder_name(results[0].dat_name)
        playlist = game_dir / f"{dat_name}.m3u"
        for path in existing_m3u:
            if path == playlist:
                continue
            self._log(f"cleanup remove: {path.name}")
            path.unlink(missing_ok=True)

        lines = [entry.name for entry in entries]
        self._log(f"writing m3u: {playlist.name}")
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _playlist_entries_for_suffix(results: list[ExtractResult], suffix: str) -> list[Path]:
        selected: list[Path] = []
        for result in results:
            matches = [p for p in result.extracted_files if p.suffix.lower() == suffix and p.exists()]
            if not matches:
                return []
            selected.append(matches[0])

        selected.sort(key=lambda p: (parse_disc_number(p.stem) or 9999, p.name.lower()))
        return selected

    def _enforce_dat_names(self, extracted: list[Path], dat_entry: DatEntry) -> list[Path]:
        renamed: list[Path] = []

        expected_cue = [r.name for r in dat_entry.roms if Path(r.name).suffix.lower() == ".cue"]
        expected_iso = [r.name for r in dat_entry.roms if Path(r.name).suffix.lower() == ".iso"]
        expected_bins = [r.name for r in dat_entry.roms if Path(r.name).suffix.lower() == ".bin"]

        cue_files = [p for p in extracted if p.suffix.lower() == ".cue"]
        iso_files = [p for p in extracted if p.suffix.lower() == ".iso"]
        bin_files = sorted([p for p in extracted if p.suffix.lower() == ".bin"], key=self._track_sort_key)

        for path in cue_files:
            target_name = expected_cue[0] if expected_cue else path.name
            if path.name != target_name:
                self._log(f"rename cue: {path.name} -> {target_name}")
            renamed.append(rename_if_needed(path, target_name, dry_run=self.dry_run))

        for path in iso_files:
            target_name = expected_iso[0] if expected_iso else path.name
            if path.name != target_name:
                self._log(f"rename iso: {path.name} -> {target_name}")
            renamed.append(rename_if_needed(path, target_name, dry_run=self.dry_run))

        for idx, path in enumerate(bin_files):
            if idx < len(expected_bins):
                target_name = expected_bins[idx]
            elif len(expected_bins) == 1:
                target_name = expected_bins[0]
            else:
                target_name = path.name
            if path.name != target_name:
                self._log(f"rename bin: {path.name} -> {target_name}")
            renamed.append(rename_if_needed(path, target_name, dry_run=self.dry_run))

        return renamed

    def _verify_dat_names(self, extracted: list[Path], dat_entry: DatEntry) -> None:
        expected_names = {
            r.name
            for r in dat_entry.roms
            if Path(r.name).suffix.lower() in {".cue", ".bin", ".iso"}
        }
        actual_names = {p.name for p in extracted if p.suffix.lower() in {".cue", ".bin", ".iso"}}

        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        if missing or extra:
            raise RuntimeError(
                "DAT filename verification failed. "
                f"missing={missing if missing else []}, extra={extra if extra else []}"
            )

    @staticmethod
    def _track_sort_key(path: Path) -> tuple[int, str]:
        text = path.stem.lower()
        marker = "track "
        if marker in text:
            tail = text.split(marker, 1)[1]
            digits = ""
            for ch in tail:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                return (int(digits), path.name)
        return (9999, path.name)

    @staticmethod
    def _primary_payload_file(extracted: list[Path]) -> Path | None:
        iso_files = [p for p in extracted if p.suffix.lower() == ".iso"]
        if iso_files:
            return iso_files[0]
        bin_files = sorted([p for p in extracted if p.suffix.lower() == ".bin"], key=ExtractWorkflow._track_sort_key)
        if bin_files:
            return bin_files[0]
        return None

    @staticmethod
    def _payload_files_from_extracted(extracted: list[Path]) -> list[PayloadFile]:
        iso_files = [p for p in extracted if p.suffix.lower() == ".iso" and p.exists()]
        if iso_files:
            return payload_files_from_paths([iso_files[0]], "iso")

        bin_files = sorted(
            [p for p in extracted if p.suffix.lower() == ".bin" and p.exists()],
            key=ExtractWorkflow._track_sort_key,
        )
        return payload_files_from_paths(bin_files, "bin")

    @staticmethod
    def _find_first_bin_from_cue(cue_path: Path) -> Path | None:
        for path in ExtractWorkflow._bin_files_from_cue(cue_path):
            return path
        return None

    @staticmethod
    def _looks_like_cue_descriptor(path: Path) -> bool:
        if not path.exists() or path.stat().st_size > 16 * 1024:
            return False
        try:
            snippet = path.read_text(encoding="utf-8", errors="ignore")[:2048].lower()
        except OSError:
            return False
        markers = ["file \"", "track ", "datafile \"", "cd_rom"]
        return any(marker in snippet for marker in markers)

    @staticmethod
    def _bin_files_from_cue(cue_path: Path) -> list[Path]:
        bins: list[Path] = []
        if not cue_path.exists():
            return bins
        for line in cue_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if not (lower.startswith("file") or lower.startswith("datafile")):
                continue

            match = re.search(r'"([^"]+)"', stripped)
            if not match:
                continue
            candidate = cue_path.parent / match.group(1)
            if candidate.suffix.lower() == ".bin":
                bins.append(candidate)
        return bins
