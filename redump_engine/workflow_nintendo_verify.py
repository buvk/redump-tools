"""Read actual Nintendo images, independently of stored manifest checksums."""
from pathlib import Path

from .dolphin_ops import DolphinTool
from .events import ResultRecord
from .library import item_for
from .metadata import DatIndex
from .verification_results import integrity_pass


def verify_nintendo_image(path: Path, tool: DolphinTool, index: DatIndex | None, *, integrity_only: bool = False) -> ResultRecord:
    system = item_for(path).system
    available = bool(index and any(entry.system in {"gamecube", "wii"} and (system is None or entry.system == system) for entry in index.entries))
    if not integrity_only and not available:
        raise ValueError("Redump DAT unavailable for this system. Download DATs before verifying contents, or use Check image integrity.")
    report = tool.verify(path)
    if report.problems:
        return ResultRecord(path.name, "Failed", "DolphinTool found image integrity problems.\n\n" + report.report,
                            path.parent, source=path, summary="Image integrity problems found.")
    if integrity_only:
        return integrity_pass(path, path, report.report)
    assert index is not None
    candidates = [entry for entry in index.by_sha1.get(report.sha1, [])
                  if entry.system in {"gamecube", "wii"} and (system is None or entry.system == system)
                  and len(entry.roms) == 1 and entry.roms[0].sha1 and entry.roms[0].sha1.lower() == report.sha1
                  and (not entry.roms[0].crc or entry.roms[0].crc.lower() == report.crc32)]
    names = {(entry.system, entry.name) for entry in candidates}
    if len(names) != 1:
        message = "Image checksums do not match a unique GameCube/Wii Redump entry."
        return ResultRecord(path.name, "Failed", message + "\n\n" + report.report, path.parent, source=path, summary=message)
    title = next(iter(names))[1]
    return ResultRecord(title, "Passed", f"{title}\nImage verified against Redump.\n\nChecksums were calculated directly from the disc image. No ISO restoration was needed.\n\n" + report.report,
                        path.parent, source=path, summary="Disc contents match Redump DAT.", check_mode="image")
