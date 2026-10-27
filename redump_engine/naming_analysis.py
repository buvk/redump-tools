"""Shared checksum-based naming decisions for audits, previews, and repairs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .filenames import dat_folder_name, is_xiso, safe_name
from .metadata import DatEntry, DatIndex, Manifest
from .payload_matching import complete_matches


def payload_filename(payload: dict[str, Any], entry: DatEntry) -> str:
    matches = [rom for rom in entry.roms if not rom.name.lower().endswith(".cue")
               and ((rom.sha1 and rom.sha1.casefold() == str(payload.get("sha1", "")).casefold())
                    or (not rom.sha1 and rom.crc and rom.crc.casefold() == str(payload.get("crc32", "")).casefold()))]
    if len(matches) > 1:
        name = str(payload.get("name", ""))
        exact = [rom for rom in matches if rom.name.casefold() == name.casefold()]
        if len(exact) == 1:
            matches = exact
        else:
            track = re.search(r"\(Track\s+0*(\d+)\)", name, re.IGNORECASE)
            if track:
                numbered = [rom for rom in matches if (candidate := re.search(r"\(Track\s+0*(\d+)\)", rom.name, re.IGNORECASE))
                            and int(candidate[1]) == int(track[1])]
                if len(numbered) == 1:
                    matches = numbered
    if len(matches) != 1:
        candidates = ", ".join(rom.name for rom in matches[:6]) or "no checksum match"
        raise ValueError(f"{entry.name}: cannot identify the original filename for '{payload.get('name', '<unnamed>')}'. "
                         f"DAT candidates: {candidates}")
    return safe_name(matches[0].name)


@dataclass(frozen=True)
class NamingAnalysis:
    entries: dict[int, DatEntry]
    unresolved: tuple[int, ...]

    @property
    def titles(self) -> set[str]:
        return {dat_folder_name(entry.name) for entry in self.entries.values()}

    def require_single_game(self) -> str:
        if self.unresolved:
            raise ValueError(f"Disc {self.unresolved[0]} needs a unique Redump checksum match before fixing names")
        if not self.entries:
            raise ValueError("The manifest has no disc records")
        if len(self.titles) != 1:
            raise ValueError("Manifest contains different games; split their records before fixing layout")
        return next(iter(self.titles))

    def image_name(self, source: Path, disc: int) -> str:
        extension = ".xiso.iso" if is_xiso(source) else source.suffix.lower()
        return safe_name(self.entries[disc].name) + extension


def analyze_manifest_names(manifest: Manifest, index: DatIndex | None) -> NamingAnalysis:
    entries = {}
    unresolved = []
    for disc in manifest.disc_entries:
        matches = complete_matches(index, disc.payload_files, manifest.system) if index else []
        if len(matches) == 1:
            entries[disc.disc] = matches[0]
        else:
            unresolved.append(disc.disc)
    return NamingAnalysis(entries, tuple(unresolved))
