from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .chd_ops import Chdman
from .cuesheets import CueRepository
from .metadata import load_dat_index
from .updater import available_systems, update_assets
from .workflow_convert import ConvertWorkflow
from .workflow_extract import ExtractWorkflow
from .workflow_verify import VerifyWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redump CHD workflow engine")

    parser.add_argument("--root", type=Path, default=Path("."), help="Workspace root containing platform folders")
    parser.add_argument("--dats", type=Path, default=Path("dats"), help="Directory with DAT files or DAT ZIP archives")
    parser.add_argument("--cuesheets", type=Path, default=Path("cuesheets"), help="Directory with cuesheet files or cue ZIP archives")
    parser.add_argument("--chdman", type=Path, default=Path("tools") / "chdman.exe", help="Path to chdman executable")
    parser.add_argument("--dry-run", action="store_true", help="Print intended actions without modifying files")
    parser.add_argument("--verbose", action="store_true", help="Print detailed decision and action logs")

    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Extract CHD files to ISO or BIN/CUE")
    extract.add_argument("--game-dir", type=Path, help="Process one specific game directory")
    extract.add_argument("--dry-run", action="store_true", help="Print intended actions without modifying files")
    extract.add_argument("--verbose", action="store_true", help="Print detailed decision and action logs")

    convert = subparsers.add_parser("convert", help="Convert ISO/BIN/CUE files to CHD")
    convert.add_argument("--game-dir", type=Path, help="Process one specific game directory")
    convert.add_argument("--dry-run", action="store_true", help="Print intended actions without modifying files")
    convert.add_argument("--verbose", action="store_true", help="Print detailed decision and action logs")

    verify = subparsers.add_parser("verify", help="Verify manifest payload checksums against DAT entries")
    verify.add_argument("--game-dir", type=Path, help="Process one specific game directory")
    verify.add_argument("--verbose", action="store_true", help="Print detailed decision and action logs")
    verify.add_argument("--csv", type=Path, help="Optional CSV output path for verify results")

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
    update.add_argument("--dry-run", action="store_true", help="Print intended actions without modifying files")
    update.add_argument("--verbose", action="store_true", help="Print detailed download logs")

    return parser


def _resolve(root: Path, child: Path) -> Path:
    return child if child.is_absolute() else root / child


def _iter_game_dirs(root: Path) -> list[Path]:
    game_dirs: list[Path] = []
    for platform in sorted(root.iterdir()):
        if not platform.is_dir():
            continue
        if platform.name.lower() in {"dats", "cuesheets", "tools", "redump_engine", "scripts", "__pycache__"}:
            continue
        for game in sorted(platform.iterdir()):
            if game.is_dir():
                game_dirs.append(game)
    return game_dirs


def _looks_like_single_game_dir(path: Path) -> bool:
    if (path / "manifest.json").exists():
        return True
    patterns = ("*.chd", "*.cue", "*.iso", "*.bin")
    return any(any(path.glob(pattern)) for pattern in patterns)


def _expand_game_dir(target: Path) -> list[Path]:
    if not target.exists() or not target.is_dir():
        return [target]
    if _looks_like_single_game_dir(target):
        return [target]

    children = [child for child in sorted(target.iterdir()) if child.is_dir()]
    if children:
        return children
    return [target]


def run() -> int:
    parser = build_parser()
    args = parser.parse_args()

    root = args.root.resolve()
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
            action = "planned" if args.dry_run else "downloaded"
            print(f"[update] {action} {result.system} {result.kind}: {result.destination}")
        return 0

    chdman_path = _resolve(root, args.chdman).resolve()
    dat_index = load_dat_index(dats)
    chdman = Chdman(chdman_path, dry_run=args.dry_run)

    if args.game_dir:
        game_dirs = _expand_game_dir((_resolve(root, args.game_dir)).resolve())
    else:
        game_dirs = _iter_game_dirs(root)

    if args.command == "extract":
        workflow = ExtractWorkflow(
            chdman=chdman,
            dat_index=dat_index,
            cues=CueRepository(cuesheets),
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        for game_dir in game_dirs:
            if not any(game_dir.glob("*.chd")):
                continue
            results = workflow.process_game_dir(game_dir)
            for result in results:
                print(
                    f"[extract] {result.game_dir.name}: "
                    f"strategy={result.strategy}, decision={result.decision}, extracted={len(result.extracted_files)}"
                )
        return 0

    if args.command == "convert":
        workflow = ConvertWorkflow(chdman=chdman, dat_index=dat_index, dry_run=args.dry_run, verbose=args.verbose)
        for game_dir in game_dirs:
            report = workflow.process_game_dir(game_dir)
            if report.get("status") == "converted":
                print(f"[convert] {game_dir.name}: discs={report['discs']} dat_name={report['dat_name']}")
        return 0

    if args.command == "verify":
        workflow = VerifyWorkflow(dat_index=dat_index, verbose=args.verbose)
        passed = 0
        failed = 0
        skipped = 0
        csv_rows: list[dict[str, str]] = []
        for game_dir in game_dirs:
            result = workflow.process_game_dir(game_dir)
            if result.status == "skipped":
                skipped += 1
                csv_rows.append(
                    {
                        "game_dir": str(game_dir),
                        "status": result.status,
                        "manifest_dat_name": result.dat_name,
                        "matched_dat_name": result.matched_dat_name or "",
                        "details": "; ".join(result.details),
                    }
                )
                continue
            if result.status == "passed":
                passed += 1
                print(
                    f"[verify] PASS {game_dir.name}: "
                    f"manifest={result.dat_name} matched={result.matched_dat_name}"
                )
                csv_rows.append(
                    {
                        "game_dir": str(game_dir),
                        "status": result.status,
                        "manifest_dat_name": result.dat_name,
                        "matched_dat_name": result.matched_dat_name or "",
                        "details": "; ".join(result.details),
                    }
                )
                continue

            failed += 1
            reason = "; ".join(result.details) if result.details else "verification failed"
            print(f"[verify] FAIL {game_dir.name}: {reason}")
            csv_rows.append(
                {
                    "game_dir": str(game_dir),
                    "status": result.status,
                    "manifest_dat_name": result.dat_name,
                    "matched_dat_name": result.matched_dat_name or "",
                    "details": "; ".join(result.details),
                }
            )

        if args.csv:
            csv_path = _resolve(root, args.csv).resolve()
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "game_dir",
                        "status",
                        "manifest_dat_name",
                        "matched_dat_name",
                        "details",
                    ],
                )
                writer.writeheader()
                writer.writerows(csv_rows)
            print(f"[verify] csv written: {csv_path}")

        checked = passed + failed
        if checked == 0:
            print("[verify] no manifest.json files found")
            print(f"[verify] summary: checked=0 passed=0 failed=0 skipped={skipped}")
            return 0

        print(f"[verify] summary: checked={checked} passed={passed} failed={failed} skipped={skipped}")
        return 2 if failed else 0

    parser.print_help()
    return 1
