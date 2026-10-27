"""Shared verification identities and successful integrity reports."""
from pathlib import Path

from .actions import VerificationMode
from .events import ResultRecord

INTEGRITY_SUMMARY = "Image integrity checked; no problems found."
INTEGRITY_SCOPE = "This check does not compare disc contents against Redump DAT."

CACHE_PREFIXES = {
    VerificationMode.MANIFEST: "Manifest",
    VerificationMode.ORIGINALS: "Original files",
    VerificationMode.CHD: "CHD",
    VerificationMode.IMAGE: "Disc image",
    VerificationMode.IMAGE_INTEGRITY: "Image integrity",
}

PASSED_RESULT_LABELS: dict[str, str] = {
    VerificationMode.MANIFEST: "✓ Manifest verified",
    VerificationMode.ORIGINALS: "✓ Original files verified",
    VerificationMode.CHD: "✓ Integrity passed",
    VerificationMode.IMAGE: "✓ Contents verified",
    VerificationMode.IMAGE_INTEGRITY: "✓ Integrity passed",
}


def cache_label(result: ResultRecord, mode: VerificationMode) -> str:
    # Cache dependencies use the action's mode; the reported check can be more
    # specific, such as an integrity-only result from an image-checking tool.
    reported = VerificationMode.IMAGE_INTEGRITY if result.check_mode == VerificationMode.IMAGE_INTEGRITY else mode
    return f"{CACHE_PREFIXES[reported]}: {result.status}"


def integrity_pass(path: Path, source: Path | None, report: str = "") -> ResultRecord:
    details = f"{INTEGRITY_SUMMARY}\n\n{INTEGRITY_SCOPE}"
    if report:
        details += "\n\n" + report
    return ResultRecord(path.name, "Passed", details, path.parent, source=source,
                        summary=INTEGRITY_SUMMARY, check_mode=VerificationMode.IMAGE_INTEGRITY)


def set_verification(labels: list[str], numbers: list[int], total_discs: int) -> str:
    """Keep grouped successes explicit about DAT versus integrity-only checks."""
    if any(label.endswith(": Failed") for label in labels):
        return "! Failed"
    if any(label.endswith(": Warning") for label in labels):
        return "! Needs attention"
    dat_prefixes = {CACHE_PREFIXES[mode] for mode in (VerificationMode.MANIFEST, VerificationMode.ORIGINALS, VerificationMode.IMAGE)} | {"Original manifest"}
    integrity_prefixes = {CACHE_PREFIXES[mode] for mode in (VerificationMode.CHD, VerificationMode.IMAGE_INTEGRITY)}
    by_disc: dict[int, list[str]] = {}
    for label, number in zip(labels, numbers, strict=True):
        by_disc.setdefault(number, []).append(label)
    passed = []
    for checks in by_disc.values():
        if not all(label.endswith(": Passed") for label in checks):
            continue
        prefixes = {label.partition(":")[0] for label in checks}
        passed.append("dat" if prefixes <= dat_prefixes else "integrity" if prefixes <= integrity_prefixes else "mixed")
    if not passed:
        return "○ Not checked"
    scope = "DAT matched" if set(passed) == {"dat"} else "integrity checked" if set(passed) == {"integrity"} else "checks passed (mixed)"
    return f"✓ All {scope}" if len(passed) == total_discs else f"{len(passed)}/{total_discs} {scope}"
