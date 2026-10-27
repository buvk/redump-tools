"""Disposable JSON cache of parsed DAT records, invalidated by source changes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .dat_models import DatEntry, DatRom
from .file_identity import file_fingerprint

CACHE_NAME = ".redump-index-v1.json"
CACHE_VERSION = 3


def source_signature(folder: Path) -> list[list[str | int]]:
    sources = []
    for path in sorted(folder.iterdir(), key=lambda path: path.name.lower()):
        if path.is_file() and path.suffix.lower() in {".dat", ".xml", ".zip"}:
            sources.append([path.name, *file_fingerprint(path)])
    return sources


def read_cache(folder: Path, signature: list[list[str | int]]) -> list[DatEntry] | None:
    path = folder / CACHE_NAME
    if path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != CACHE_VERSION or data["sources"] != signature or not isinstance(data["entries"], list):
            return None
        entries = []
        for value in data["entries"]:
            name, system, roms = value
            if not isinstance(name, str) or not (system is None or isinstance(system, str)) or not isinstance(roms, list):
                return None
            records = []
            for record in roms:
                filename, size, crc, sha1 = record
                if (not isinstance(filename, str) or not (size is None or type(size) is int and size >= 0)
                        or not all(value is None or isinstance(value, str) for value in (crc, sha1))):
                    return None
                records.append(DatRom(filename, size, crc, sha1))
            entries.append(DatEntry(name, records, system))
        return entries
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_cache(folder: Path, signature: list[list[str | int]], entries: list[DatEntry]) -> None:
    temporary: Path | None = None
    try:
        if (folder / CACHE_NAME).is_symlink() or source_signature(folder) != signature:
            return
        data = {"version": CACHE_VERSION, "sources": signature,
                "entries": [[entry.name, entry.system, [[rom.name, rom.size, rom.crc, rom.sha1] for rom in entry.roms]] for entry in entries]}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, prefix=".redump-index_", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, separators=(",", ":"), ensure_ascii=False)
        os.replace(temporary, folder / CACHE_NAME)
    except OSError:
        pass  # Read-only reference folders still support verification.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
