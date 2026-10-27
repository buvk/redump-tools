from __future__ import annotations

import argparse
import json
from pathlib import Path

from .actions import VerificationMode, resolve_action
from .batch import _expand_game_dir, _iter_game_dirs
from .chd_ops import Chdman
from .cuesheets import CueRepository
from .events import EventReporter, ToolProgress
from .execution import ExecutionOptions, WorkflowExecutor, run_batch
from .file_batch import run_file_batch
from .library import is_manifest, scan_library, selected_paths
from .metadata import DatIndex, load_dat_index
from .paths import CUESHEET_ROOT, DAT_ROOT
from .systems import refresh_manifest_systems
from .updater import available_systems, update_assets
from .workflow_delete import delete_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redump CHD workflow engine")

    parser.add_argument("--root", type=Path, default=Path("."), help="Workspace root containing platform folders")
    parser.add_argument("--dats", type=Path, default=DAT_ROOT, help="Directory with DAT files or DAT ZIP archives")
    parser.add_argument("--cuesheets", type=Path, default=CUESHEET_ROOT, help="Directory with cuesheet files or cue ZIP archives")
    parser.add_argument("--chdman", type=Path, default=Path("tools") / "chdman.exe", help="Path to chdman executable")
    parser.add_argument("--dolphin-tool", type=Path, default=Path("tools") / "DolphinTool.exe", help="Path to DolphinTool executable")
    parser.add_argument("--dry-run", action="store_true", help="Print intended actions without modifying files")
    parser.add_argument("--verbose", action="store_true", help="Print detailed decision and action logs")
    parser.add_argument("--force", action="store_true", help="Allow chdman to overwrite existing output files")

    # Subcommand copies of the global flags use SUPPRESS so they never reset a value given before the subcommand.
    def shared_flag(sub: argparse.ArgumentParser, name: str, help_text: str) -> None:
        sub.add_argument(name, action="store_true", default=argparse.SUPPRESS, help=help_text)

    dry_run_help = "Print intended actions without modifying files"
    verbose_help = "Print detailed decision and action logs"
    force_help = "Allow chdman to overwrite existing output files"

    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Extract CHD files to ISO or BIN/CUE")
    extract.add_argument("--game-dir", type=Path, help="Process one specific game directory")
    shared_flag(extract, "--dry-run", dry_run_help)
    shared_flag(extract, "--verbose", verbose_help)
    shared_flag(extract, "--force", force_help)

    convert = subparsers.add_parser("convert", help="Convert ISO/BIN/CUE files to CHD")
    convert.add_argument("--game-dir", type=Path, help="Process one specific game directory")
    convert.add_argument("--use-redump-names", action="store_true", help="Use checksum-matched Redump names for both CHDs and their game folder")
    shared_flag(convert, "--dry-run", dry_run_help)
    shared_flag(convert, "--verbose", verbose_help)
    shared_flag(convert, "--force", force_help)

    for sub in (extract, convert):
        sub.add_argument("--keep-sources", action="store_true", help="Keep original disc images after processing")
        sub.add_argument("--no-rename-folder", action="store_true", help="Keep the game folder name")

    verify = subparsers.add_parser("verify", help="Verify manifest payload checksums against DAT entries")
    verify.add_argument("--game-dir", type=Path, help="Process one specific game directory")
    shared_flag(verify, "--verbose", verbose_help)
    verify.add_argument("--csv", type=Path, help="Optional CSV output path for verify results")
    verify_modes = verify.add_mutually_exclusive_group()
    verify_modes.add_argument("--chd-integrity", action="store_true", help="Check compressed CHDs with chdman verify; does not compare payloads with DATs")
    verify_modes.add_argument(
        "--rehash",
        action="store_true",
        help="Also re-hash the payload files on disk and compare them with the manifest",
    )

    update = subparsers.add_parser("update", help="Download current DAT and cuesheet ZIP archives")
    update.add_argument(
        "--systems",
        nargs="+",
        choices=available_systems(),
        default=list(available_systems()),
        help="Systems to update",
    )
    update.add_argument("--no-dats", action="store_true", help="Skip DAT downloads")
    update.add_argument("--no-cuesheets", action="store_true", help="Skip cuesheet downloads")
    shared_flag(update, "--dry-run", dry_run_help)
    shared_flag(update, "--verbose", "Print detailed download logs")

    manifest = subparsers.add_parser("manifest", help="Create missing game manifests; CHDs are extracted temporarily")
    manifest.add_argument("--game-dir", type=Path, help="Scan a folder for missing manifests")
    shared_flag(manifest, "--dry-run", dry_run_help)
    shared_flag(manifest, "--verbose", verbose_help)
    manifest.add_argument("--refresh-system", action="store_true", help="Fill existing manifest system fields from recognized console folders")

    organize = subparsers.add_parser("organize", help="Organize game folders, checksum-matched names, and manifest references")
    organize.add_argument("--game-dir", type=Path, help="Scan a library folder")
    shared_flag(organize, "--dry-run", dry_run_help)
    shared_flag(organize, "--verbose", verbose_help)

    repair = subparsers.add_parser("repair-layout", help="Fix checksum-matched Redump names and consolidate game manifests")
    repair.add_argument("--game-dir", type=Path, help="Scan a library folder")
    shared_flag(repair, "--dry-run", dry_run_help)
    shared_flag(repair, "--verbose", verbose_help)

    xiso = subparsers.add_parser("xiso", help="Rewrite original Xbox ISOs as XISO; originals cannot be reconstructed")
    xiso.add_argument("--game-dir", type=Path, help="Scan for ISOs; only use with original Xbox images")
    xiso.add_argument("--extract-xiso", type=Path, default=Path("tools") / "extract-xiso.exe", help="Path to extract-xiso executable")
    xiso.add_argument("--delete-source", action="store_true", help="Delete original ISO after success (irreversible)")
    shared_flag(xiso, "--dry-run", dry_run_help)
    shared_flag(xiso, "--verbose", verbose_help)
    shared_flag(xiso, "--force", "Overwrite existing output files")
    shared_flag(xiso, "--use-redump-names", "Use Redump file and folder names after a unique checksum match")

    rvz = subparsers.add_parser("rvz", help="Compress GameCube/Wii ISOs losslessly to RVZ")
    rvz_extract = subparsers.add_parser("rvz-extract", help="Restore RVZ images to ISO")
    for sub in (rvz, rvz_extract):
        sub.add_argument("--game-dir", type=Path, help="Scan a library folder")
        sub.add_argument("--delete-source", action="store_true", help="Delete source only after successful verification")
        shared_flag(sub, "--dry-run", dry_run_help)
        shared_flag(sub, "--verbose", verbose_help)
        shared_flag(sub, "--force", "Overwrite existing output files")
        shared_flag(sub, "--use-redump-names", "Use Redump file and folder names after a unique checksum match")

    delete = subparsers.add_parser("delete", help="Permanently delete an explicit file selection")
    delete.add_argument("--confirm-delete", action="store_true", help="Confirm permanent deletion of the listed files")
    shared_flag(delete, "--verbose", verbose_help)
    shared_flag(delete, "--dry-run", dry_run_help)
    image_verify = subparsers.add_parser("nintendo-verify", help="Hash actual GameCube/Wii ISO or RVZ images and compare with Redump")
    image_integrity = subparsers.add_parser("nintendo-integrity", help="Check actual GameCube/Wii images with DolphinTool")
    chd_verify = subparsers.add_parser("chd-verify", help="Temporarily extract CHD contents and compare with Redump DATs")
    for sub in (image_verify, image_integrity, chd_verify):
        sub.add_argument("--game-dir", type=Path, help="Scan a library folder")
        shared_flag(sub, "--verbose", verbose_help)

    # Path options are accepted after the subcommand as well as before it.
    for sub in (extract, convert, verify, update, manifest, organize, repair, xiso, rvz, rvz_extract, delete, image_verify, image_integrity, chd_verify):
        sub.add_argument("--approved-plans", type=Path, help=argparse.SUPPRESS)
        sub.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
        sub.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
        if sub is not update:
            sub.add_argument("--game-list", type=Path, help=argparse.SUPPRESS)
            sub.add_argument("--file-list", type=Path, help=argparse.SUPPRESS)
        sub.add_argument("--root", type=Path, default=argparse.SUPPRESS, help="Workspace root or platform folder")
        sub.add_argument("--dats", type=Path, default=argparse.SUPPRESS, help="Directory with DAT files or ZIP archives")
        sub.add_argument("--cuesheets", type=Path, default=argparse.SUPPRESS, help="Directory with cuesheet files or ZIP archives")
        sub.add_argument("--chdman", type=Path, default=argparse.SUPPRESS, help="Path to chdman executable")
        sub.add_argument("--dolphin-tool", type=Path, default=argparse.SUPPRESS, help="Path to DolphinTool executable")

    return parser


def _resolve(root: Path, child: Path) -> Path:
    """Resolve a relative path against the root, falling back to the working directory."""
    if child.is_absolute():
        return child
    candidate = root / child
    if candidate.exists():
        return candidate
    fallback = Path.cwd() / child
    return fallback if fallback.exists() else candidate


def _clean_path_args(args: argparse.Namespace) -> None:
    """Drop stray quotes left by Windows parsing of a trailing backslash, e.g. "C:\\dir\\"."""
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, Path(str(value).replace('"', "").strip()))


def _load_dats(folder: Path, events: EventReporter) -> DatIndex | None:
    events.tool_progress(ToolProgress("Loading Redump DATs", None, None, "Preparing checksum index", "started"))
    index = load_dat_index(folder)
    events.tool_progress(ToolProgress("Loading Redump DATs", None, None, "Ready", "finished"))
    return index


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _clean_path_args(args)
    approved_plans = None
    if args.approved_plans is not None:
        if args.command not in {"organize", "repair-layout"} or not args.file_list:
            parser.error("Approved plans require an explicit organization file selection")
        try:
            approved_plans = json.loads(args.approved_plans.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            parser.error(f"Cannot read approved organization plans: {error}")
        if not isinstance(approved_plans, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in approved_plans.items()):
            parser.error("Invalid approved organization plans")

    events = EventReporter(args.events)
    if args.command == "convert" and args.use_redump_names and args.no_rename_folder:
        parser.error("--use-redump-names renames both files and folder; it cannot be combined with --no-rename-folder")

    if args.command == "verify" and args.chd_integrity and args.csv:
        parser.error("--csv is currently supported only for manifest/DAT verification")
    if args.command == "verify" and args.file_list and args.csv:
        parser.error("Use the desktop's Export CSV action for selected-file verification")

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"--root is not an existing directory: {root}")
    dats = _resolve(root, args.dats).resolve()
    cuesheets = _resolve(root, args.cuesheets).resolve()

    if args.command == "update":
        include_dats = not args.no_dats
        include_cuesheets = not args.no_cuesheets
        if not include_dats and not include_cuesheets:
            parser.error("update requires at least one of DATs or cuesheets to remain enabled")

        results = update_assets(
            dats_dir=dats,
            cuesheets_dir=cuesheets,
            systems=args.systems,
            include_dats=include_dats,
            include_cuesheets=include_cuesheets,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        for result in results:
            if result.error:
                print(f"[update] FAILED {result.system} {result.kind}: {result.error}")
                events.result(f"{result.system} {result.kind}", "Failed", result.error, reference=result.destination, system=result.system, operation="update")
                continue
            action = "planned" if args.dry_run else "downloaded"
            print(f"[update] {action} {result.system} {result.kind}: {result.destination}")
            events.result(f"{result.system} {result.kind}", "Planned" if args.dry_run else "Updated",
                          str(result.destination), reference=result.destination, system=result.system, operation="update")
        return 1 if any(result.error for result in results) else 0

    if args.command == "manifest" and args.refresh_system:
        if args.file_list or args.game_list:
            parser.error("--refresh-system requires --game-dir, not a file/game list")
        target = _resolve(root, args.game_dir).resolve() if args.game_dir else root
        if not target.is_dir():
            parser.error(f"Not an existing library folder: {target}")
        results = refresh_manifest_systems(target, args.dry_run)
        for path, status, detail in results:
            if args.verbose or status == "Failed":
                print(f"[manifest] {status.upper()} {path}: {detail}")
            events.result(path.name, status, detail, folder=path.parent, source=path)
        counts = {status: sum(row[1] == status for row in results) for status in ("Updated", "Planned", "Skipped", "Failed")}
        print(f"[manifest] system refresh: {counts}")
        events.summary(processed=counts["Updated"] + counts["Planned"], errors=counts["Failed"], skipped=counts["Skipped"])
        return 2 if counts["Failed"] else 0

    if args.command == "delete":
        if not args.file_list or args.game_list or not args.confirm_delete:
            parser.error("delete requires --file-list and --confirm-delete; folders are never deleted")
        try:
            values = json.loads(args.file_list.read_text(encoding="utf-8"))
            if not isinstance(values, list) or not values or not all(isinstance(value, str) and Path(value).is_file() and not Path(value).is_symlink() for value in values):
                raise ValueError("File list must contain existing files, not folders or symbolic links")
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid deletion selection: {exc}")
        return delete_files(list(dict.fromkeys(Path(value).resolve() for value in values)), events, args.stop_file, dry_run=args.dry_run)

    chdman_path = _resolve(root, args.chdman).resolve()
    chdman = Chdman(chdman_path, dry_run=args.dry_run, force=args.force, progress=True,
                    on_progress=events.tool_progress)

    if args.command in {"manifest", "organize", "repair-layout", "xiso", "rvz", "rvz-extract", "nintendo-verify", "nintendo-integrity", "chd-verify"} and not args.file_list:
        if args.game_list:
            parser.error(f"{args.command} accepts --game-dir or an explicit file selection, not --game-list")
        target = _resolve(root, args.game_dir).resolve() if args.game_dir else root
        if not target.is_dir():
            parser.error(f"Not an existing library folder: {target}")
        paths = selected_paths(scan_library(target), args.command)
        return run_file_batch(paths, args.command, chdman, None if args.command == "nintendo-integrity" else _load_dats(dats, events), CueRepository(cuesheets),
                              keep_sources=not getattr(args, "delete_source", False), dry_run=args.dry_run, verbose=args.verbose, rehash=False,
                              check_chds=False, stop_file=args.stop_file, events=events,
                              extract_xiso=_resolve(root, args.extract_xiso).resolve() if args.command == "xiso" else None,
                              use_redump_names=getattr(args, "use_redump_names", False),
                              dolphin_tool=_resolve(root, args.dolphin_tool).resolve(), approved_plans=approved_plans, dats=dats)

    if args.file_list:
        try:
            values = json.loads(args.file_list.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read selected files: {exc}")
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and Path(value).is_file() and
                (Path(value).suffix.lower() in {".chd", ".rvz", ".iso", ".xiso", ".cue", ".bin"} or is_manifest(Path(value))) for value in values):
            parser.error("file list must contain existing disc images or manifests")
        paths = list(dict.fromkeys(Path(value).resolve() for value in values))
        return run_file_batch(paths, args.command, chdman,
                              None if args.command == "nintendo-integrity" or args.command == "verify" and args.chd_integrity else _load_dats(dats, events),
                              CueRepository(cuesheets), keep_sources=not args.delete_source if args.command in {"xiso", "rvz", "rvz-extract"} else getattr(args, "keep_sources", True),
                              dry_run=args.dry_run, verbose=args.verbose, rehash=getattr(args, "rehash", False),
                              check_chds=getattr(args, "chd_integrity", False), stop_file=args.stop_file, events=events,
                              extract_xiso=_resolve(root, args.extract_xiso).resolve() if args.command == "xiso" else None,
                              use_redump_names=getattr(args, "use_redump_names", False),
                              dolphin_tool=_resolve(root, args.dolphin_tool).resolve(), approved_plans=approved_plans, dats=dats)

    if args.game_list:
        values = json.loads(args.game_list.read_text(encoding="utf-8"))
        if not isinstance(values, list) or not all(isinstance(value, str) and Path(value).is_dir() for value in values):
            parser.error("game list must contain existing game folder paths")
        game_dirs = list(dict.fromkeys(Path(value).resolve() for value in values))
    elif args.game_dir:
        game_dirs = _expand_game_dir((_resolve(root, args.game_dir)).resolve())
    else:
        game_dirs = _iter_game_dirs(root)

    action = resolve_action(args.command, rehash=getattr(args, "rehash", False), check_chds=getattr(args, "chd_integrity", False))
    options = ExecutionOptions(args.dry_run, args.verbose, getattr(args, "keep_sources", True),
                               rename_folder=not getattr(args, "no_rename_folder", False),
                               use_redump_names=getattr(args, "use_redump_names", False), dats=dats)
    dat_index = None if action.verification == VerificationMode.CHD else _load_dats(dats, events)
    executor = WorkflowExecutor(action, chdman, dat_index, CueRepository(cuesheets), options, events, selected=False)
    csv_path = _resolve(root, args.csv).resolve() if getattr(args, "csv", None) else None
    return run_batch(game_dirs, executor, events, stop_file=args.stop_file, csv_path=csv_path)
