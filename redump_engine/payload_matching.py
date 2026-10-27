"""Match complete payload multisets, preserving duplicate tracks and system scope."""
from typing import Any

from .dat_models import DatEntry, DatIndex, DatRom


def _compatible(payload: dict[str, Any], rom: DatRom) -> bool:
    if rom.size is not None and payload.get("size") != rom.size:
        return False
    compared = False
    for key, expected in (("sha1", rom.sha1), ("crc32", rom.crc)):
        actual = payload.get(key)
        if expected and actual:
            compared = True
            if str(actual).casefold() != expected.casefold():
                return False
    return compared


def _all_payloads_match(payloads: list[dict[str, Any]], roms: list[DatRom]) -> bool:
    owners: dict[int, int] = {}

    def assign(number: int, visited: set[int]) -> bool:
        for target, rom in enumerate(roms):
            if target in visited or not _compatible(payloads[number], rom):
                continue
            visited.add(target)
            if target not in owners or assign(owners[target], visited):
                owners[target] = number
                return True
        return False

    return all(assign(number, set()) for number in range(len(payloads)))


def complete_matches(index: DatIndex, payloads: list[dict[str, Any]], system: str | None = None) -> list[DatEntry]:
    payloads = [payload for payload in payloads if not str(payload.get("name", "")).lower().endswith(".cue")]
    sha1s = {str(payload["sha1"]).lower() for payload in payloads if payload.get("sha1")}
    crc32s = {str(payload["crc32"]).lower() for payload in payloads if payload.get("crc32")}
    matches = []
    candidates = {id(entry): entry for checksum in sha1s for entry in index.by_sha1.get(checksum, [])}
    candidates.update({id(entry): entry for checksum in crc32s for entry in index.by_crc32.get(checksum, [])})
    for entry in candidates.values():
        if system and entry.system and entry.system != system:
            continue
        roms = [rom for rom in entry.roms if not rom.name.lower().endswith(".cue")]
        if not payloads or len(roms) != len(payloads):
            continue
        if _all_payloads_match(payloads, roms):
            matches.append(entry)
    return matches
