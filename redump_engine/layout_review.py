"""Prepare a collection repair review without depending on the desktop UI."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from .dat_loader import load_dat_index
from .events import ResultRecord
from .layout_contract import plan_contract
from .workflow_layout import LayoutPlan, plan_layout
from .workflow_organize import plan_organization


@dataclass(frozen=True)
class ReviewProgress:
    current: int
    total: int
    path: Path


@dataclass(frozen=True)
class LayoutReview:
    descriptions: tuple[str, ...]
    failures: tuple[ResultRecord, ...]
    planned: int
    full_descriptions: tuple[str, ...] = ()
    approved_plans: dict[str, str] = field(default_factory=dict)

    @property
    def display(self) -> list[str]:
        return [*self.descriptions, *(f"Will not change:\n{record.source}\n{record.details}" for record in self.failures)]


def prepare_layout_review(files: list[Path], dats: Path, cancelled: Event,
                          progress: Callable[[ReviewProgress], None], *, organize: bool = False) -> LayoutReview | None:
    if cancelled.is_set():
        return None
    index = load_dat_index(dats)
    plans = []
    failures = []
    approved = {}
    seen_contracts = set()
    for number, path in enumerate(files, 1):
        if cancelled.is_set():
            return None
        progress(ReviewProgress(number, len(files), path))
        try:
            plan = (plan_organization if organize else plan_layout)(path, index)
            if organize:
                contract = plan_contract(plan)
                if contract in seen_contracts:
                    continue
                seen_contracts.add(contract)
                approved[str(path.resolve())] = contract
            plans.append(plan)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(ResultRecord(path.parent.name, "Failed", str(exc), path.parent, source=path))
    if cancelled.is_set():
        return None
    return LayoutReview(group_descriptions(plans), tuple(failures), sum(bool(plan.changed) for plan in plans), group_descriptions(plans, full_paths=True), approved)


def group_descriptions(plans: list[LayoutPlan], *, full_paths: bool = False) -> tuple[str, ...]:
    """Group review text only; each executable plan retains its own safeguards."""
    games: dict[tuple[Path, str], list[str]] = {}
    for plan in plans:
        text = plan.description(full_paths=True) if full_paths else plan.description()
        blocks = text.split("\n\n")
        key = (plan.folder, plan.title)
        if key not in games:
            games[key] = [blocks[0]]
        for block in blocks[1:]:
            if block not in games[key]:
                games[key].append(block)
    return tuple("\n\n".join(blocks) for blocks in games.values())
