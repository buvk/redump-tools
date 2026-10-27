"""Versioned JSON-line messages between the batch process and desktop UI.

Human-readable logs are deliberately outside this protocol.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PREFIX = "@redump-event "


@dataclass(frozen=True)
class ResultRecord:
    name: str
    status: str
    details: str
    folder: Path | None = None
    reference: Path | None = None
    source: Path | None = None
    summary: str | None = None
    suggested_action: str | None = None
    check_mode: str | None = None
    verified_inputs: dict[str, dict[str, Any]] | None = None
    system: str | None = None
    operation: str | None = None

    @property
    def columns(self) -> tuple[str, str, str]:
        return self.name, self.status, self.details


@dataclass(frozen=True)
class ToolProgress:
    operation: str
    source: Path | None
    percent: float | None
    message: str
    state: str = "running"


@dataclass(frozen=True)
class BatchEvent:
    kind: str
    current: int = 0
    total: int = 0
    folder: Path | None = None
    result: ResultRecord | None = None
    counts: dict[str, int] = field(default_factory=dict)
    tool: ToolProgress | None = None
    previous_folder: Path | None = None
    path_changes: dict[Path, Path] = field(default_factory=dict)


def decode_event(line: str) -> BatchEvent | None:
    if not line.startswith(PREFIX):
        return None
    try:
        data = json.loads(line[len(PREFIX):])
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        kind = data.get("kind")
        if kind == "progress":
            current, total, folder = data.get("current"), data.get("total"), data.get("folder")
            if type(current) is int and type(total) is int and 1 <= current <= total and isinstance(folder, str) and folder:
                return BatchEvent(kind, current, total, Path(folder))
        elif kind == "folder-renamed":
            previous, folder = data.get("previous"), data.get("folder")
            if isinstance(previous, str) and previous and isinstance(folder, str) and folder:
                return BatchEvent(kind, folder=Path(folder), previous_folder=Path(previous))
        elif kind == "paths-renamed":
            changes = data.get("changes")
            if isinstance(changes, dict) and changes and all(isinstance(old, str) and old and isinstance(new, str) and new for old, new in changes.items()):
                return BatchEvent(kind, path_changes={Path(old): Path(new) for old, new in changes.items()})
        elif kind == "tool-progress":
            operation, source, percent = data.get("operation"), data.get("source"), data.get("percent")
            message, state = data.get("message"), data.get("state")
            if not isinstance(operation, str) or not operation or not isinstance(message, str):
                return None
            if state not in {"started", "running", "finished", "failed"}:
                return None
            if source is not None and (not isinstance(source, str) or not source):
                return None
            if percent is not None and (type(percent) not in {int, float} or not math.isfinite(percent) or not 0 <= percent <= 100):
                return None
            return BatchEvent(kind, tool=ToolProgress(operation, Path(source) if source else None, percent, message, state))
        elif kind == "result":
            name, status, details = data.get("name"), data.get("status"), data.get("details")
            folder, reference, source = data.get("folder"), data.get("reference"), data.get("source")
            summary, suggested, check_mode = data.get("summary"), data.get("suggested_action"), data.get("check_mode")
            system, operation = data.get("system"), data.get("operation")
            if not isinstance(name, str) or not isinstance(status, str) or not isinstance(details, str):
                return None
            if status not in {"Passed", "Failed", "Skipped", "Completed", "Planned", "Updated", "Warning"}:
                return None
            if any(value is not None and (not isinstance(value, str) or not value) for value in (folder, reference, source, summary, suggested, check_mode, system, operation)):
                return None
            inputs = data.get("verified_inputs")
            if inputs is not None and (not isinstance(inputs, dict) or not all(isinstance(key, str) and isinstance(value, dict) for key, value in inputs.items())):
                return None
            return BatchEvent(kind, result=ResultRecord(name, status, details,
                              Path(folder) if folder else None, Path(reference) if reference else None, Path(source) if source else None,
                              summary, suggested, check_mode, inputs, system, operation))
        elif kind == "summary":
            counts = data.get("counts")
            if isinstance(counts, dict) and all(isinstance(key, str) and type(value) is int and value >= 0 for key, value in counts.items()):
                return BatchEvent(kind, counts=counts)
        elif kind == "stopped":
            return BatchEvent(kind)
    except (ValueError, TypeError):
        pass
    return None


class EventReporter:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def emit(self, kind: str, **data: Any) -> None:
        if self.enabled:
            print(PREFIX + json.dumps({"version": 1, "kind": kind, **data}), flush=True)

    def result(self, name: str, status: str, details: str, folder: Path | None = None,
               reference: Path | None = None, source: Path | None = None, *, summary: str | None = None,
               suggested_action: str | None = None, check_mode: str | None = None, verified_inputs: dict[str, dict[str, Any]] | None = None, system: str | None = None, operation: str | None = None) -> None:
        self.emit("result", name=name, status=status, details=details,
                  folder=str(folder.resolve()) if folder else None,
                  reference=str(reference.resolve()) if reference else None,
                  source=str(source.resolve()) if source else None, summary=summary, suggested_action=suggested_action, check_mode=check_mode, verified_inputs=verified_inputs, system=system, operation=operation)

    def tool_progress(self, update: ToolProgress) -> None:
        self.emit("tool-progress", operation=update.operation,
                  source=str(update.source.resolve()) if update.source else None,
                  percent=update.percent, message=update.message, state=update.state)

    def summary(self, **counts: int) -> None:
        self.emit("summary", counts=counts)
