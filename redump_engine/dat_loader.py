"""Load DAT/XML archives and build checksum indexes, using disposable caches."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from zipfile import ZipFile

from .dat_cache import read_cache, source_signature, write_cache
from .dat_models import DatEntry, DatIndex, DatRom
from .filenames import normalize_name
from .systems import recognize_system


def parse_dat_xml(dat_text: str) -> list[DatEntry]:
    root = ET.fromstring(dat_text)
    system = recognize_system(root.findtext("header/name", "")) or recognize_system(root.findtext("header/description", ""))
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
        entries.append(DatEntry(name=name, roms=roms, system=system))
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

    signature = source_signature(dat_dir)
    entries = read_cache(dat_dir, signature)
    if entries is None:
        entries = []
        for payload in _iter_dat_payloads(dat_dir):
            try:
                entries.extend(parse_dat_xml(payload))
            except ET.ParseError:
                continue
        write_cache(dat_dir, signature, entries)

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

    if source_signature(dat_dir) != signature:
        raise ValueError("DAT files changed while loading; run the operation again")
    return DatIndex(entries=entries, by_normalized_name=by_normalized_name, by_sha1=by_sha1, by_crc32=by_crc32,
                    source_inputs=(str(dat_dir.resolve()), signature))
