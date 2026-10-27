"""Selection summaries and full disc details independent of the library widgets."""
from __future__ import annotations

from collections.abc import Hashable, Iterable

from .formatting import format_size
from .game_manifests import disc_for_source
from .library_models import LibraryItem
from .metadata import load_manifest
from .systems import SYSTEM_NAMES
from .verification_results import set_verification


def _original_totals(items: list[LibraryItem], identities: Iterable[Hashable]) -> tuple[int, int]:
    """Count each proven original disc once across its image representations."""
    originals: dict[Hashable, int] = {}
    discs = set()
    for item, identity in zip(items, identities, strict=True):
        discs.add(identity)
        if item.original_size is not None:
            originals[identity] = max(originals.get(identity, 0), item.original_size)
    return sum(originals.values()), len(discs - originals.keys())


def set_sizes(items: list[LibraryItem], numbers: list[int]) -> tuple[int, int | None, float | None]:
    """Numeric totals shared by set display and sorting."""
    current = sum(item.size for item in items)
    total, unknown = _original_totals(items, numbers)
    original = None if unknown else total
    return current, original, 100 * (original - current) / original if original is not None and original > 0 else None


def set_summary(items: list[LibraryItem], labels: list[str], numbers: list[int], total_discs: int) -> tuple[str, str, str, str, str]:
    """Summarize visible set members without reading files or averaging ratios."""
    current, original, percentage = set_sizes(items, numbers)
    has_originals = any(item.original_size is not None for item in items)
    original_text = format_size(original) if original is not None else "Partial" if has_originals else "Unknown"
    savings = "Partial" if has_originals else "Unknown"
    if original is not None and percentage is not None:
        saved = original - current
        savings = f"{format_size(abs(saved))} ({100 * abs(saved) / original:.1f}%{' larger' if saved < 0 else ''})"
    manifest = "✓ Recorded" if all(not item.missing_record for item in items) else "! Partial" if any(item.manifest for item in items) else "○ Missing"
    verification = set_verification(labels, numbers, total_discs)
    return format_size(current), original_text, savings, manifest, verification


def selection_summary(items: list[LibraryItem]) -> str:
    discs = list({item.path: item for item in items if item.kind != "Manifest"}.values())
    if not discs:
        return "" if not items else f"{len(items)} manifests selected"
    current = sum(item.size for item in discs)
    seen = {item.path for item in discs}
    for item in discs:
        for track in item.dependencies:
            if track in seen:
                try:
                    current -= track.stat().st_size
                except OSError:
                    pass
            seen.add(track)
    original, unknown = _original_totals(discs, (item.original_id or item.path for item in discs))
    original_text = (f"{format_size(original)} + {unknown} unknown" if original else "Unknown") if unknown else format_size(original)
    text = f"Current: {format_size(current)}  ·  Original: {original_text}"
    if not unknown and original > 0:
        saved = original - current
        label = "Saved" if saved >= 0 else "Larger by"
        text += f"  ·  {label}: {format_size(abs(saved))} ({100 * abs(saved) / original:.1f}%)"
    return text


def disc_details(item: LibraryItem, verification: str) -> str:
    system = SYSTEM_NAMES.get(item.system, item.system) if item.system else "Unknown"
    lines = [item.path.name, "", f"System: {system}", f"Format: {item.kind}", f"Verification: {verification}"]
    if verification.endswith(": Warning"):
        lines.append("Recorded checksums match Redump; file names or folder layout need attention. Use Fix names and folders to review changes.")
    lines.extend(["", selection_summary([item]), "", f"File: {item.path}", f"Game folder: {item.path.parent}",
                  f"Manifest: {item.manifest or 'Missing'}"])
    if item.manifest:
        lines.append("Manifest record: " + ("Incomplete" if item.missing_record else "Recorded"))
        try:
            manifest = load_manifest(item.manifest)
            disc = disc_for_source(item.path, manifest) if manifest else None
            if manifest:
                lines.append(f"Recorded game: {manifest.title}")
                lines.append(f"Discs recorded: {len(manifest.disc_entries)}")
            if disc:
                lines.append(f"Disc number: {disc.disc}")
                if disc.source_files:
                    lines.extend(["", "Associated images:", *disc.source_files])
                if disc.payload_files:
                    lines.extend(["", "Original disc records (not compressed-image checksums):"])
                    for payload in disc.payload_files:
                        lines.append(str(payload.get("name", "Unnamed payload")))
                        for key, caption in (("size", "Size (bytes)"), ("sha1", "SHA-1"), ("crc32", "CRC32")):
                            if payload.get(key) is not None:
                                lines.append(f"  {caption}: {payload[key]}")
        except (OSError, ValueError, KeyError, TypeError):
            lines.append("Manifest could not be read; refresh the library to check its current state.")
    if item.problem:
        lines.append(item.problem)
    if item.dependencies:
        lines.extend(["", "Included tracks:", *(str(path) for path in item.dependencies)])
    if item.kind == "XISO":
        lines.append("Xbox XISO removes disc data; the original Redump ISO cannot be reconstructed. Manifest verification checks the original ISO records, not the XISO image.")
    return "\n".join(lines)
