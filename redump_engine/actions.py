"""Shared action identities, capabilities, and presentation for CLI and desktop."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class VerificationMode(StrEnum):
    NONE = "none"
    MANIFEST = "manifest"
    ORIGINALS = "originals"
    CHD = "chd"
    IMAGE = "image"
    IMAGE_INTEGRITY = "image-integrity"


@dataclass(frozen=True)
class ActionSpec:
    id: str
    command: str
    label: str
    running_label: str
    group: str | None = None
    kinds: frozenset[str] | None = None
    systems: frozenset[str] | None = None
    tool: str | None = None
    verification: VerificationMode = VerificationMode.NONE
    modifies_sources: bool = False
    allows_overwrite: bool = False
    system_kinds: tuple[tuple[str, frozenset[str]], ...] = ()
    input_tools: tuple[tuple[str, str], ...] = ()
    success_summary: str | None = None

    @property
    def targets_manifest(self) -> bool:
        return self.id == "repair-layout" or self.verification in {VerificationMode.MANIFEST, VerificationMode.ORIGINALS}

    def supports(self, kind: str, system: str | None) -> bool:
        if any(system == console and kind not in kinds for console, kinds in self.system_kinds):
            return False
        return (self.kinds is None or kind in self.kinds) and (system is None or self.systems is None or system in self.systems)

    def required_tools(self, files: list[Path]) -> tuple[str, ...]:
        suffixes = {path.suffix.lower() for path in files}
        tools = ([self.tool] if self.tool else []) + [tool for suffix, tool in self.input_tools if suffix in suffixes]
        return tuple(dict.fromkeys(tools))

    def summary_for(self, status: str) -> str | None:
        if status == "Planned":
            return f"{self.label.rstrip('…')}: preview only."
        return self.success_summary if status in {"Passed", "Completed"} else None


CHD_SYSTEMS = frozenset({"psx", "ps2", "dc"})
GROUPS = (("manifests", "Manifests"), ("chd", "CHD (PlayStation / Dreamcast)"), ("xbox", "Xbox"),
          ("nintendo", "GameCube / Wii (RVZ)"))
_SPECS = (
    ActionSpec("verify", "verify", "Verify manifest against Redump DAT", "Verifying manifests", "manifests", verification=VerificationMode.MANIFEST),
    ActionSpec("manifest", "manifest", "Create missing manifests…", "Creating manifests", "manifests", frozenset({"CHD", "RVZ", "ISO", "CUE/BIN"}),
               input_tools=((".chd", "chdman"), (".rvz", "DolphinTool")), success_summary="Manifest created; original checksums recorded."),
    ActionSpec("rehash", "verify", "Verify original files against Redump DAT", "Verifying original files", verification=VerificationMode.ORIGINALS),
    ActionSpec("repair-layout", "repair-layout", "Fix naming/layout…", "Fixing naming/layout",
               modifies_sources=True),
    ActionSpec("convert", "convert", "Convert to CHD", "Converting to CHD", "chd", frozenset({"ISO", "CUE/BIN"}), CHD_SYSTEMS,
               "chdman", modifies_sources=True, allows_overwrite=True,
               system_kinds=(("dc", frozenset({"CUE/BIN"})),), success_summary="CHD created; see details for paths."),
    ActionSpec("extract", "extract", "Restore CHD to ISO / BIN+CUE", "Restoring CHD", "chd", frozenset({"CHD"}), CHD_SYSTEMS,
               "chdman", modifies_sources=True, allows_overwrite=True,
               success_summary="CHD restored; see details for paths."),
    ActionSpec("chd-integrity", "verify", "Check image integrity", "Checking CHD integrity", "chd", frozenset({"CHD"}),
               tool="chdman", verification=VerificationMode.CHD),
    ActionSpec("chd-verify", "chd-verify", "Verify contents against Redump DAT", "Verifying disc contents", "chd",
               frozenset({"CHD"}), CHD_SYSTEMS, "chdman", verification=VerificationMode.IMAGE),
    ActionSpec("xiso", "xiso", "Convert ISO to XISO…", "Creating Xbox XISOs", "xbox", frozenset({"ISO"}), frozenset({"xbox"}),
               "extract-xiso", modifies_sources=True, allows_overwrite=True, success_summary="XISO created; original checksums recorded."),
    ActionSpec("rvz", "rvz", "Convert ISO to RVZ", "Compressing RVZ", "nintendo", frozenset({"ISO"}),
               frozenset({"gamecube", "wii"}), "DolphinTool", modifies_sources=True, allows_overwrite=True,
               success_summary="RVZ created and verified; see details for paths."),
    ActionSpec("rvz-extract", "rvz-extract", "Restore RVZ to ISO", "Restoring ISO", "nintendo", frozenset({"RVZ"}),
               frozenset({"gamecube", "wii"}), "DolphinTool", modifies_sources=True, allows_overwrite=True,
               success_summary="ISO restored; see details for paths."),
    ActionSpec("nintendo-integrity", "nintendo-integrity", "Check image integrity", "Checking disc image", "nintendo",
               frozenset({"ISO", "RVZ"}), frozenset({"gamecube", "wii"}), "DolphinTool", verification=VerificationMode.IMAGE_INTEGRITY),
    ActionSpec("nintendo-verify", "nintendo-verify", "Verify contents against Redump DAT", "Verifying disc contents", "nintendo",
               frozenset({"ISO", "RVZ"}), frozenset({"gamecube", "wii"}), "DolphinTool", verification=VerificationMode.IMAGE),
    ActionSpec("organize", "organize", "Fix names and folders…", "Fixing names and folders", kinds=frozenset({"CHD", "RVZ", "ISO", "XISO", "CUE/BIN", "Manifest"}),
               success_summary="Names and layout organized; see details for paths."),
    ActionSpec("delete", "delete", "Delete selected…", "Deleting files", kinds=frozenset({"CHD", "RVZ", "ISO", "XISO", "CUE/BIN", "BIN (no CUE)", "Manifest"})),
)
ACTIONS = {spec.id: spec for spec in _SPECS}
CHDMAN_ACTIONS = {"createcd": "convert", "createdvd": "convert", "extractcd": "extract", "extractdvd": "extract", "verify": "chd-integrity"}


def resolve_action(operation: str, *, rehash: bool = False, check_chds: bool = False) -> ActionSpec:
    """Normalize legacy CLI verification flags at the boundary, never from display text."""
    if rehash and check_chds:
        raise ValueError("Original-file and CHD integrity verification are mutually exclusive")
    if operation == "verify":
        operation = "chd-integrity" if check_chds else "rehash" if rehash else operation
    try:
        return ACTIONS[operation]
    except KeyError:
        raise ValueError(f"Unknown action: {operation}") from None
