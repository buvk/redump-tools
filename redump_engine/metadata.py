from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, cast
from zipfile import ZipFile

from .filenames import normalize_name, parse_disc_number

PayloadValue = str | int
PayloadFile = dict[str, PayloadValue]


def _empty_dat_roms() -> list[DatRom]:
    return []


def _empty_payload_files() -> list[PayloadFile]:
    return []


def _empty_manifest_discs() -> list[ManifestDisc]:
    return []


def _empty_dat_entries_by_checksum() -> dict[str, list[DatEntry]]:
    return {}


def payload_file_from_path(path: Path, kind: str) -> PayloadFile:
    return {
        "name": path.name,
        "sha1": compute_sha1(path),
        "crc32": compute_crc32(path),
        "size": path.stat().st_size,
        "kind": kind,
    }


def payload_files_from_paths(paths: Iterable[Path], kind: str) -> list[PayloadFile]:
    payloads: list[PayloadFile] = []
    for path in paths:
        if not path.exists():
            continue
        payloads.append(payload_file_from_path(path, kind))
    return payloads


def payload_checksums_from_payload_files(payload_files: Iterable[PayloadFile]) -> tuple[set[str], set[str]]:
    sha1s: set[str] = set()
    crc32s: set[str] = set()

    for payload in payload_files:
        kind = str(payload.get("kind", "")).lower()
        if kind not in {"iso", "bin"}:
            continue
        sha1 = str(payload.get("sha1", "")).strip().lower()
        crc32 = str(payload.get("crc32", "")).strip().lower()
        if sha1:
            sha1s.add(sha1)
        if crc32:
            crc32s.add(crc32)

    return sha1s, crc32s


def has_rev_tag(entry: DatEntry) -> bool:
    return bool(re.search(r"\(rev[^)]*\)", entry.name, flags=re.IGNORECASE))


def has_unresolved_name_ambiguity(
    name_dat_matches: list[DatEntry],
    checksum_dat_matches: list[DatEntry],
) -> bool:
    return len(name_dat_matches) > 1 and len(checksum_dat_matches) != 1


def describe_dat_match_ambiguity(
    source_name: str,
    name_dat_matches: list[DatEntry],
    checksum_dat_matches: list[DatEntry],
) -> str:
    checksum_names = ", ".join(entry.name for entry in checksum_dat_matches) if checksum_dat_matches else "none"
    name_matches = ", ".join(entry.name for entry in name_dat_matches)
    return (
        "ambiguous DAT title match remains unresolved: "
        f"source={source_name}, name_candidates=[{name_matches}], checksum_candidates=[{checksum_names}]"
    )


def select_dat_entry(
    name_dat_matches: list[DatEntry],
    checksum_dat_matches: list[DatEntry],
    desired_format: str | None = None,
) -> DatEntry | None:
    name_dat_entry = name_dat_matches[0] if name_dat_matches else None
    checksum_dat_entry = checksum_dat_matches[0] if checksum_dat_matches else None

    if name_dat_entry is None:
        return checksum_dat_entry
    if checksum_dat_entry is None:
        return name_dat_entry

    if len(checksum_dat_matches) == 1:
        return checksum_dat_entry

    if desired_format:
        name_matches = name_dat_entry.expected_format == desired_format
        checksum_matches = checksum_dat_entry.expected_format == desired_format
        if checksum_matches and not name_matches:
            return checksum_dat_entry
        if name_matches and not checksum_matches:
            return name_dat_entry

    checksum_has_rev = has_rev_tag(checksum_dat_entry)
    name_has_rev = has_rev_tag(name_dat_entry)
    if checksum_has_rev and not name_has_rev:
        return checksum_dat_entry
    if name_has_rev and not checksum_has_rev:
        return name_dat_entry

    return name_dat_entry


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
            if r.sha1 and Path(r.name).suffix.lower() != ".cue"
        }

    @property
    def payload_crc_set(self) -> set[str]:
        return {
            r.crc.lower()
            for r in self.roms
            if r.crc and Path(r.name).suffix.lower() != ".cue"
        }


@dataclass
class DatIndex:
    entries: list[DatEntry]
    by_normalized_name: dict[str, list[DatEntry]]
    by_sha1: dict[str, list[DatEntry]] = field(default_factory=_empty_dat_entries_by_checksum)
    by_crc32: dict[str, list[DatEntry]] = field(default_factory=_empty_dat_entries_by_checksum)

    @staticmethod
    def _candidate_base_name(candidate: str) -> str:
        raw = Path(candidate).name.strip()
        suffix = Path(raw).suffix.lower()
        # Only strip known file/image extensions. Keep dots inside plain game titles.
        known_suffixes = {".chd", ".cue", ".bin", ".iso"}
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


@dataclass
class ManifestDisc:
    disc: int
    payload_files: list[PayloadFile] = field(default_factory=_empty_payload_files)

    @property
    def track_count(self) -> int:
        bins = [p for p in self.payload_files if str(p.get("kind", "")).lower() == "bin"]
        return max(len(bins), 1)


@dataclass
class Manifest:
    schema_version: int
    set_type: str
    title: str
    disc_entries: list[ManifestDisc] = field(default_factory=_empty_manifest_discs)

    @property
    def dat_name(self) -> str:
        return self.title

    @property
    def disc_count(self) -> int:
        if self.disc_entries:
            return max(d.disc for d in self.disc_entries)
        return 0

    def disc_entry_for(self, disc_number: int) -> ManifestDisc | None:
        for disc in self.disc_entries:
            if disc.disc == disc_number:
                return disc
        return None

    def to_dict(self) -> dict[str, object]:
        discs: list[dict[str, object]] = [
            {
                "disc": disc.disc,
                "payload_files": disc.payload_files,
            }
            for disc in self.disc_entries
        ]
        return {
            "schema_version": self.schema_version,
            "set_type": self.set_type,
            "title": self.title,
            "discs": discs,
        }


def compute_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def compute_crc32(path: Path, chunk_size: int = 1024 * 1024) -> str:
    import zlib

    crc = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08x}"


def load_manifest(manifest_path: Path) -> Manifest | None:
    if not manifest_path.exists():
        return None
    payload = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))

    if "set_type" not in payload or "title" not in payload or "discs" not in payload:
        raise ValueError(
            f"unsupported manifest schema in {manifest_path}; expected schema_version/set_type/title/discs"
        )

    raw_discs = cast(list[dict[str, Any]], payload.get("discs", []))
    disc_entries: list[ManifestDisc] = []
    for item in raw_discs:
        payload_files = cast(list[PayloadFile], item.get("payload_files", []))
        disc_entries.append(
            ManifestDisc(
                disc=int(item["disc"]),
                payload_files=list(payload_files),
            )
        )

    return Manifest(
        schema_version=int(payload.get("schema_version", 1)),
        set_type=str(payload.get("set_type", "bin-cue")),
        title=str(payload.get("title", "")),
        disc_entries=disc_entries,
    )


def save_manifest(manifest_path: Path, manifest: Manifest) -> None:
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")


def match_dat_entry(name: str, dat_index: DatIndex | None) -> DatEntry | None:
    if dat_index is None:
        return None
    return dat_index.match_by_name(name)


def match_dat_entries(name: str, dat_index: DatIndex | None) -> list[DatEntry]:
    if dat_index is None:
        return []
    return dat_index.name_matches(name)


def match_dat_entry_for_files(paths: list[Path], dat_index: DatIndex | None) -> DatEntry | None:
    matches = match_dat_entries_for_files(paths, dat_index)
    if not matches:
        return None
    return matches[0]


def match_dat_entries_for_files(paths: list[Path], dat_index: DatIndex | None) -> list[DatEntry]:
    if dat_index is None:
        return []

    payload_paths = [
        p for p in paths if p.exists() and p.suffix.lower() in {".bin", ".iso"}
    ]
    if not payload_paths:
        return []

    sha1s = {compute_sha1(path).lower() for path in payload_paths}
    crc32s = {compute_crc32(path).lower() for path in payload_paths}
    return dat_index.checksum_matches(sha1s=sha1s, crc32s=crc32s)


def _parse_dat_xml(dat_text: str) -> list[DatEntry]:
    root = ET.fromstring(dat_text)
    entries: list[DatEntry] = []
    for game in root.findall(".//game"):
        name = game.attrib.get("name")
        if not name:
            continue
        roms: list[DatRom] = []
        for rom in game.findall("rom"):
            roms.append(
                DatRom(
                    name=rom.attrib.get("name", ""),
                    size=int(rom.attrib["size"]) if "size" in rom.attrib else None,
                    crc=rom.attrib.get("crc"),
                    sha1=rom.attrib.get("sha1"),
                )
            )
        entries.append(DatEntry(name=name, roms=roms))
    return entries


def _iter_dat_payloads(dat_dir: Path) -> Iterable[str]:
    for item in sorted(dat_dir.iterdir(), key=lambda path: path.name.lower()):
        if item.is_file() and item.suffix.lower() in {".dat", ".xml"}:
            yield item.read_text(encoding="utf-8", errors="ignore")
        elif item.is_file() and item.suffix.lower() == ".zip":
            with ZipFile(item) as archive:
                for member in sorted(archive.namelist(), key=str.lower):
                    if member.lower().endswith((".dat", ".xml")):
                        with archive.open(member) as handle:
                            yield handle.read().decode("utf-8", errors="ignore")


def load_dat_index(dat_dir: Path) -> DatIndex | None:
    if not dat_dir.exists():
        return None

    entries: list[DatEntry] = []
    for payload in _iter_dat_payloads(dat_dir):
        try:
            entries.extend(_parse_dat_xml(payload))
        except ET.ParseError:
            continue

    by_normalized_name: dict[str, list[DatEntry]] = {}
    by_sha1: dict[str, list[DatEntry]] = {}
    by_crc32: dict[str, list[DatEntry]] = {}
    for entry in entries:
        key = normalize_name(entry.name)
        by_normalized_name.setdefault(key, []).append(entry)
        for checksum in entry.payload_sha1_set:
            by_sha1.setdefault(checksum, []).append(entry)
        for checksum in entry.payload_crc_set:
            by_crc32.setdefault(checksum, []).append(entry)

    return DatIndex(entries=entries, by_normalized_name=by_normalized_name, by_sha1=by_sha1, by_crc32=by_crc32)
