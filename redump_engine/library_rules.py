"""Action eligibility and selection rules, independent of library scanning."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .actions import VerificationMode, resolve_action
from .filenames import dat_folder_name, disc_title, safe_name
from .game_folders import is_game_folder
from .library_metadata import manifest_for
from .metadata import load_manifest

if TYPE_CHECKING:
    from .library_models import LibraryItem


def unavailable_reason(item: LibraryItem, operation: str, check_chds: bool = False) -> str:
    """One eligibility decision shared by action execution and UI explanations."""
    try:
        action = resolve_action(operation, check_chds=check_chds)
    except ValueError:
        return "Unknown action"
    if not action.supports(item.kind, item.system):
        if action.kinds is not None and item.kind not in action.kinds:
            return "Requires " + " or ".join(sorted(action.kinds))
        return "Unavailable for this system"
    if action.verification == VerificationMode.CHD or action.id == "delete":
        return ""
    if action.targets_manifest:
        if item.manifest is None:
            return "Manifest missing"
        if item.missing_record:
            return "Disc record missing from manifest"
        if action.verification == VerificationMode.ORIGINALS and not original_payloads_available(item.manifest):
            return "Original ISO/BIN files missing; extract first"
        return ""
    if item.problem:
        return item.problem
    if action.id == "manifest":
        return "" if item.missing_record else "Manifest already exists"
    if action.id == "organize":
        if item.manifest is not None:
            return ""
        try:
            return "Already in a game folder" if organize_destination(item.path) == item.path.parent else ""
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return "Cannot determine a safe game folder"
    return ""


def eligible(item: LibraryItem, operation: str, check_chds: bool = False) -> bool:
    return not unavailable_reason(item, operation, check_chds)


def original_payloads_available(manifest_path: Path) -> bool:
    """Rehash checks the entire manifest, so every recorded payload must be present."""
    try:
        manifest = load_manifest(manifest_path)
        if manifest is None or not manifest.disc_entries:
            return False
        root = manifest_path.parent.resolve()
        for disc in manifest.disc_entries:
            if not disc.payload_files:
                return False
            for payload in disc.payload_files:
                name = str(payload.get("name", ""))
                path = (root / name).resolve()
                if not name or not path.is_relative_to(root) or not path.is_file():
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def organize_destination(path: Path) -> Path:
    title = dat_folder_name(disc_title(path))
    manifest_path = manifest_for(path)
    if manifest_path:
        manifest = load_manifest(manifest_path)
        if manifest:
            title = dat_folder_name(manifest.title)
    title = safe_name(title)
    if is_game_folder(path.parent, title):
        return path.parent
    destination = path.parent / title
    if destination.resolve() != destination or destination.resolve().parent != path.parent.resolve():
        raise ValueError("Destination must stay within the selected file's folder")
    return destination


def selected_paths(items: list[LibraryItem], operation: str, check_chds: bool = False) -> list[Path]:
    action = resolve_action(operation, check_chds=check_chds)
    chosen = [item for item in items if eligible(item, action.id)]
    if action.id == "organize":
        image_manifests = {item.manifest for item in chosen if item.kind != "Manifest"}
        chosen = [item for item in chosen if item.kind != "Manifest" or item.path not in image_manifests]
    if action.id == "manifest":
        chosen.sort(key=lambda item: item.kind == "CHD")
    if action.targets_manifest:
        return list(dict.fromkeys(item.manifest or item.path for item in chosen))
    return list(dict.fromkeys(item.path for item in chosen))
