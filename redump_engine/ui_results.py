"""Result records, filtering, sorting and CSV export."""
from __future__ import annotations

import csv
from pathlib import Path

from .actions import ACTIONS
from .events import ResultRecord
from .systems import SHORT_SYSTEM_NAMES, SYSTEM_NAMES, system_from_path
from .ui_badges import result_badge
from .verification_results import PASSED_RESULT_LABELS

RESULT_HEADERS = ("Game / item", "System", "Operation", "Status", "Details")
RESULT_STATUS_COLUMN = RESULT_HEADERS.index("Status")
RESULT_DETAILS_COLUMN = RESULT_HEADERS.index("Details")


def result_context(result: ResultRecord) -> tuple[str, str]:
    path = result.source or result.reference or (result.folder / "_" if result.folder else None)
    system = result.system or (system_from_path(path) if path else None)
    operation = ACTIONS[result.operation].label.rstrip(".…") if result.operation in ACTIONS else "Refresh DATs and cuesheets" if result.operation == "update" else result.operation or "—"
    return SHORT_SYSTEM_NAMES.get(system or "", SYSTEM_NAMES.get(system or "", "Unknown")), operation


def result_summary(result: ResultRecord) -> str:
    """Keep table rows short; full diagnostics remain in details and exports."""
    if result.summary:
        return result.summary
    detail = " ".join(result.details.split())
    return detail if len(detail) <= 100 else detail[:97].rstrip() + "…"


def export_results_csv(destination: Path, results: list[ResultRecord]) -> None:
    """Export all rows, regardless of the current table filter."""
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(RESULT_HEADERS)
        writer.writerows((result_columns(row)[0], *result_context(row), row.status, row.details) for row in results)


def result_columns(result: ResultRecord) -> tuple[str, str, str, str, str]:
    """Use the same values for table display and sorting."""
    name = (result.name[:-len(".manifest.json")] if result.name.endswith(".manifest.json") else
            result.folder.name if result.folder and result.name == "manifest.json" else result.name)
    status = PASSED_RESULT_LABELS.get(result.check_mode or "", result_badge(result.status)) if result.status == "Passed" else result_badge(result.status)
    return name, *result_context(result), status, result_summary(result)


def result_details(result: ResultRecord) -> str:
    name = result_columns(result)[0]
    marker = ". all manifest discs matched DAT payload checksums"
    if result.status == "Passed" and result.details.startswith("Manifest matches Redump DAT: ") and marker in result.details:
        _, _, extra = result.details.partition(marker)
        text = f"{name}\n✓ Manifest verified against Redump\n\nAll recorded disc checksums match the DAT."
        if extra.strip(". \n"):
            text += "\n\n" + extra.lstrip(". \n")
        return text
    return f"{name}\n{result_columns(result)[RESULT_STATUS_COLUMN]}\n\n{result.details}"


def visible_result_indices(results: list[ResultRecord], status_filter: str, query: str,
                           sort_column: int, reverse: bool) -> list[int]:
    wanted = {"Failed": "Failed", "Skipped": "Skipped", "Warnings": "Warning"}.get(status_filter)
    query = query.strip().casefold()
    indices = [index for index, row in enumerate(results)
               if (wanted is None or row.status == wanted) and (not query or query in " ".join((*result_columns(row), row.details)).casefold())]
    return sorted(indices, key=lambda index: (result_columns(results[index])[sort_column].casefold(),
                                             result_columns(results[index])[0].casefold()), reverse=reverse)


def failed_folders(results: list[ResultRecord]) -> list[Path]:
    return list(dict.fromkeys(row.folder for row in results if row.status == "Failed" and row.folder is not None and row.folder.is_dir()))


def completion_summary(counts: dict[str, int], results: list[ResultRecord], *, preview: bool,
                       exit_code: int, stopped: bool, elapsed: float) -> tuple[str, str, bool]:
    """Return status text, color and log visibility from a completed batch."""
    if "checked" in counts:
        summary = f"{counts['checked']} checked · {counts.get('passed', 0)} passed · {counts.get('failed', 0)} failed · {counts.get('skipped', 0)} skipped"
    elif counts:
        total = counts.get("processed", counts.get("converted", 0))
        summary = f"{total} {'planned' if preview else 'processed'} · {counts.get('errors', 0)} errors"
        if counts.get("skipped"):
            summary += f" · {counts['skipped']} skipped"
    else:
        summary = f"{sum(row.status in {'Completed', 'Passed', 'Updated', 'Planned'} for row in results)} completed · {sum(row.status == 'Failed' for row in results)} failed"
    if not counts and not results:
        summary = "See the log for details"
    warnings = counts.get("warnings", sum(row.status == "Warning" for row in results))
    if warnings:
        summary += f" · {warnings} {'warning' if warnings == 1 else 'warnings'}"
    prefix = "Stopped safely" if stopped else "Finished with errors" if exit_code else "Finished with warnings" if warnings else "Finished"
    color = "#895600" if stopped or (warnings and not exit_code) else "#16703c" if exit_code == 0 else "#b42318"
    show_log = (not counts and not results) or (bool(exit_code) and not any(row.status == "Failed" for row in results))
    return f"{prefix}: {summary} ({elapsed:.0f}s)", color, show_log
