"""DAT records and in-memory lookup rules, independent of loading and caching."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .filenames import normalize_name, parse_disc_number


def _empty_dat_roms() -> list[DatRom]:
    return []


def _empty_dat_entries_by_checksum() -> dict[str, list[DatEntry]]:
    return {}


@dataclass
class DatRom:
    name: str
    size: int | None = None
    crc: str | None = None
    sha1: str | None = None


@dataclass
class DatEntry:
    name: str
    roms: list[DatRom] = field(default_factory=_empty_dat_roms)
    system: str | None = None

    @property
    def expected_format(self) -> str | None:
        exts = {Path(r.name).suffix.lower() for r in self.roms}
        if ".iso" in exts:
            return "iso"
        if ".cue" in exts or ".bin" in exts:
            return "bin-cue"
        return None

    @property
    def track_count(self) -> int:
        return sum(1 for r in self.roms if Path(r.name).suffix.lower() == ".bin")

    @property
    def sha1_set(self) -> set[str]:
        return {r.sha1.lower() for r in self.roms if r.sha1}

    @property
    def payload_sha1_set(self) -> set[str]:
        return {
            r.sha1.lower()
            for r in self.roms
            if r.sha1 and not r.name.lower().endswith(".cue")
        }

    @property
    def payload_crc_set(self) -> set[str]:
        return {
            r.crc.lower()
            for r in self.roms
            if r.crc and not r.name.lower().endswith(".cue")
        }


@dataclass
class DatIndex:
    entries: list[DatEntry]
    by_normalized_name: dict[str, list[DatEntry]]
    by_sha1: dict[str, list[DatEntry]] = field(default_factory=_empty_dat_entries_by_checksum)
    by_crc32: dict[str, list[DatEntry]] = field(default_factory=_empty_dat_entries_by_checksum)
    source_inputs: tuple[str, list[list[str | int]]] | None = None

    @staticmethod
    def _candidate_base_name(candidate: str) -> str:
        raw = Path(candidate).name.strip()
        suffix = Path(raw).suffix.lower()
        # Only strip known file/image extensions. Keep dots inside plain game titles.
        known_suffixes = {".chd", ".rvz", ".cue", ".bin", ".iso"}
        if suffix in known_suffixes:
            return Path(raw).stem
        return raw

    def name_matches(self, candidate: str) -> list[DatEntry]:
        base_name = self._candidate_base_name(candidate)
        key = normalize_name(base_name)
        candidate_disc = parse_disc_number(base_name)

        exact_matches = list(self.by_normalized_name.get(key, []))
        if exact_matches:
            return self._resolve_disc_candidates(exact_matches, candidate_disc)

        # Disc-aware exact fallback: strip disc token and require exact base match.
        key_no_disc = re.sub(r"disc\d+", "", key)

        candidates: list[DatEntry] = []
        for known_key, entries in self.by_normalized_name.items():
            known_no_disc = re.sub(r"disc\d+", "", known_key)
            if known_no_disc == key_no_disc:
                candidates.extend(entries)

        return self._resolve_disc_candidates(candidates, candidate_disc)

    @staticmethod
    def _resolve_disc_candidates(candidates: list[DatEntry], candidate_disc: int | None) -> list[DatEntry]:
        if not candidates:
            return []

        if candidate_disc is not None:
            disc_specific = [entry for entry in candidates if parse_disc_number(entry.name) == candidate_disc]
            if disc_specific:
                return disc_specific

        return candidates

    def match_by_name(self, candidate: str) -> DatEntry | None:
        candidates = self.name_matches(candidate)
        if not candidates:
            return None
        return candidates[0]

    def checksum_matches(self, sha1s: set[str], crc32s: set[str]) -> list[DatEntry]:
        if not sha1s and not crc32s:
            return []

        candidate_scores: dict[int, int] = {}
        candidate_entries: dict[int, DatEntry] = {}
        for checksum in sha1s:
            for entry in self.by_sha1.get(checksum, []):
                entry_id = id(entry)
                candidate_scores[entry_id] = candidate_scores.get(entry_id, 0) + 1
                candidate_entries[entry_id] = entry
        for checksum in crc32s:
            for entry in self.by_crc32.get(checksum, []):
                entry_id = id(entry)
                candidate_scores[entry_id] = candidate_scores.get(entry_id, 0) + 1
                candidate_entries[entry_id] = entry

        candidates: list[tuple[int, int, DatEntry]] = []
        for entry_id, score in candidate_scores.items():
            entry = candidate_entries[entry_id]
            entry_sha1 = entry.payload_sha1_set
            entry_crc = entry.payload_crc_set

            if sha1s and not sha1s.issubset(entry_sha1):
                continue
            if crc32s and not crc32s.issubset(entry_crc):
                continue

            payload_count = len(entry_sha1) or len(entry_crc) or 9999
            candidates.append((score, -payload_count, entry))

        if not candidates:
            return []

        # Prefer the strongest checksum overlap, then the closest payload cardinality.
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_score = candidates[0][0]
        best_payload_count = candidates[0][1]
        return [entry for score, payload_count, entry in candidates if score == best_score and payload_count == best_payload_count]

    def match_by_checksums(self, sha1s: set[str], crc32s: set[str]) -> DatEntry | None:
        candidates = self.checksum_matches(sha1s=sha1s, crc32s=crc32s)
        if not candidates:
            return None
        return candidates[0]
