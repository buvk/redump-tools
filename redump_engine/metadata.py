from __future__ import annotations

import hashlib
import json
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .dat_loader import load_dat_index as load_dat_index
from .dat_models import DatEntry as DatEntry
from .dat_models import DatIndex as DatIndex
from .dat_models import DatRom as DatRom
from .file_io import write_metadata
from .payload_paths import payload_name
from .systems import recognize_system, system_from_path

PayloadValue = str | int
PayloadFile = dict[str, PayloadValue]


def _empty_payload_files() -> list[PayloadFile]:
    return []


def _empty_manifest_discs() -> list[ManifestDisc]:
    return []


def payload_file_from_path(path: Path, kind: str, *, base: Path | None = None) -> PayloadFile:
    sha1, crc32 = compute_hashes(path)
    return {
        "name": payload_name(path, base),
        "sha1": sha1,
        "crc32": crc32,
        "size": path.stat().st_size,
        "kind": kind,
    }


def payload_files_from_paths(paths: Iterable[Path], kind: str, *, base: Path | None = None) -> list[PayloadFile]:
    payloads: list[PayloadFile] = []
    for path in paths:
        if not path.exists():
            continue
        payloads.append(payload_file_from_path(path, kind, base=base))
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
class ManifestDisc:
    disc: int
    payload_files: list[PayloadFile] = field(default_factory=_empty_payload_files)
    source_files: list[str] = field(default_factory=list)

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
    system: str | None = None

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
                **({"source_files": disc.source_files} if disc.source_files else {}),
            }
            for disc in self.disc_entries
        ]
        return {
            "schema_version": self.schema_version,
            "set_type": self.set_type,
            "title": self.title,
            "discs": discs,
            **({"system": self.system} if self.system else {}),
        }


def compute_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def compute_crc32(path: Path, chunk_size: int = 1024 * 1024) -> str:
    return compute_hashes(path, chunk_size)[1]


def compute_hashes(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, str]:
    """Return (sha1, crc32) computed in a single pass over the file."""
    digest = hashlib.sha1()
    crc = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
    return digest.hexdigest(), f"{crc & 0xFFFFFFFF:08x}"


def load_manifest(manifest_path: Path) -> Manifest | None:
    if not manifest_path.exists():
        return None
    payload = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    return manifest_from_dict(payload, manifest_path)


def manifest_from_dict(payload: dict[str, Any], source: Path | None = None) -> Manifest:
    """Decode an already-read document while callers retain unknown JSON fields."""
    if not isinstance(payload, dict) or "set_type" not in payload or "title" not in payload or "discs" not in payload:
        raise ValueError(
            f"unsupported manifest schema in {source or '<data>'}; expected schema_version/set_type/title/discs"
        )

    def invalid(detail: str) -> ValueError:
        return ValueError(f"Invalid manifest in {source or '<data>'}: {detail}")

    if not isinstance(payload["set_type"], str) or not isinstance(payload["title"], str):
        raise invalid("set_type and title must be strings")
    schema_version = payload.get("schema_version", 1)
    if type(schema_version) is not int or schema_version < 1:
        raise invalid("schema_version must be a positive integer")
    raw_discs = payload["discs"]
    if not isinstance(raw_discs, list):
        raise invalid("discs must be a list")
    disc_entries: list[ManifestDisc] = []
    numbers = set()
    for item in raw_discs:
        if not isinstance(item, dict):
            raise invalid("each disc must be an object")
        value = item.get("disc")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise invalid("disc number must be a positive integer")
        try:
            number = int(value)
        except ValueError as error:
            raise invalid("disc number must be a positive integer") from error
        if number < 1 or number in numbers:
            raise invalid("disc numbers must be positive and unique")
        numbers.add(number)
        files = item.get("payload_files", [])
        if not isinstance(files, list) or any(not isinstance(record, dict) for record in files):
            raise invalid("payload_files must be a list of objects")
        sources = item.get("source_files", [])
        if not isinstance(sources, list) or any(not isinstance(name, str) for name in sources):
            raise invalid("source_files must be a list of strings")
        disc_entries.append(ManifestDisc(number, list(files), list(sources)))

    return Manifest(
        schema_version=schema_version,
        set_type=str(payload.get("set_type", "bin-cue")),
        title=str(payload.get("title", "")),
        disc_entries=disc_entries,
        system=(recognize_system(payload["system"]) or payload["system"]) if isinstance(payload.get("system"), str) else None,
    )


def save_manifest(manifest_path: Path, manifest: Manifest) -> None:
    manifest.system = manifest.system or system_from_path(manifest_path)
    write_metadata(manifest_path, json.dumps(manifest.to_dict(), indent=2).encode("utf-8"))


def match_dat_entry(name: str, dat_index: DatIndex | None) -> DatEntry | None:
    if dat_index is None:
        return None
    return dat_index.match_by_name(name)


def match_dat_entries(name: str, dat_index: DatIndex | None, *, system: str | None = None) -> list[DatEntry]:
    if dat_index is None:
        return []
    matches = dat_index.name_matches(name)
    if system is None:
        return matches
    scoped = [entry for entry in matches if entry.system == system]
    return scoped or [entry for entry in matches if entry.system is None]


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

    sha1s: set[str] = set()
    crc32s: set[str] = set()
    for path in payload_paths:
        sha1, crc32 = compute_hashes(path)
        sha1s.add(sha1.lower())
        crc32s.add(crc32.lower())
    return dat_index.checksum_matches(sha1s=sha1s, crc32s=crc32s)
