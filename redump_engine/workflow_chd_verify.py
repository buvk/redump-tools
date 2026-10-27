"""Compare temporarily restored CHD payloads against original Redump records."""
from pathlib import Path

from .actions import CHD_SYSTEMS
from .events import ResultRecord
from .game_manifests import disc_for_source
from .library import item_for
from .metadata import load_manifest, payload_files_from_paths
from .payload_matching import complete_matches
from .workflow_manifest import ManifestWorkflow


def verify_chd_contents(path: Path, workflow: ManifestWorkflow) -> ResultRecord:
    index = workflow.dat_index
    item = item_for(path)
    system = item.system
    if index is None or not any(entry.system in CHD_SYSTEMS and (not system or entry.system == system) for entry in index.entries):
        raise ValueError("Redump DAT unavailable for this system. Download DATs before verifying contents, or use Check image integrity.")
    metadata = item.manifest
    existing = load_manifest(metadata) if metadata else None
    before = path.stat()

    def compare(originals: list[Path], kind: str) -> tuple[str, str]:
        if not originals:
            raise ValueError("No original disc payloads were extracted")
        payloads = payload_files_from_paths(originals, kind)
        matches = [entry for entry in complete_matches(index, payloads, system) if entry.system in CHD_SYSTEMS]
        names = {(entry.system, entry.name) for entry in matches}
        if len(names) != 1:
            return "Failed", "Extracted disc contents do not match a unique Redump DAT entry.\nThe CHD and library files were kept unchanged."
        name = next(iter(names))[1]
        return "Passed", f"{name}\nDisc contents verified against Redump DAT.\n\nAll {len(payloads)} original payload file(s) match the DAT by size, SHA-1 and CRC32.\nTemporary extraction files were removed; library files were not changed."

    # Existing metadata is a layout hint only. Checksums come from extracted bytes.
    if existing and disc_for_source(path, existing) is None:
        existing = None
    status, details = workflow.inspect_chd_payloads(path, compare, existing)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("CHD changed during verification; run the check again")
    return ResultRecord(path.name, status, details, path.parent, source=path,
                        summary="Disc contents match Redump DAT." if status == "Passed" else "Disc contents do not match Redump DAT.", check_mode="image")
