"""Compatibility entry point for explicitly selected disc workflows."""
from __future__ import annotations

from pathlib import Path

from .actions import resolve_action
from .chd_ops import Chdman
from .cuesheets import CueRepository
from .events import EventReporter
from .execution import ExecutionOptions, WorkflowExecutor, run_batch
from .metadata import DatIndex


def run_file_batch(paths: list[Path], operation: str, chdman: Chdman, dat_index: DatIndex | None,
                   cues: CueRepository, *, keep_sources: bool, dry_run: bool, verbose: bool,
                   rehash: bool, check_chds: bool, stop_file: Path | None, events: EventReporter,
                   extract_xiso: Path | None = None, use_redump_names: bool = False, dolphin_tool: Path | None = None, approved_plans: dict[str, str] | None = None, dats: Path | None = None) -> int:
    options = ExecutionOptions(dry_run, verbose, keep_sources, rename_folder=False, extract_xiso=extract_xiso,
                               use_redump_names=use_redump_names, dolphin_tool=dolphin_tool, approved_plans=approved_plans, dats=dats)
    executor = WorkflowExecutor(resolve_action(operation, rehash=rehash, check_chds=check_chds), chdman, dat_index, cues,
                                options, events, selected=True)
    return run_batch(paths, executor, events, stop_file=stop_file)
