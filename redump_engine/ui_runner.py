"""Desktop command construction and subprocess transport (no Tk dependency)."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .actions import ActionSpec, VerificationMode, resolve_action
from .paths import APP_ROOT


def build_command(operation: str, folder: Path, *, chdman: Path, dats: Path, cues: Path,
                  preview: bool = False, keep_sources: bool = True, force: bool = False,
                  rehash: bool = False, check_chds: bool = False, refresh_cues: bool | None = None,
                  systems: list[str] | None = None, extract_xiso: Path | None = None, use_redump_names: bool = False,
                  dolphin_tool: Path | None = None) -> list[str]:
    """Build an argument list without invoking a shell or changing CLI defaults."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    action = resolve_action(operation, rehash=rehash, check_chds=check_chds) if operation != "update" else None
    operation = action.command if action else operation
    command = [str(executable), "-u", "-m", "redump_engine", operation,
               "--root", str(APP_ROOT),
               "--chdman", str(chdman), "--dats", str(dats), "--cuesheets", str(cues), "--verbose", "--events"]
    command.extend(("--dolphin-tool", str(dolphin_tool or APP_ROOT / "tools" / "DolphinTool.exe")))
    if operation == "delete":
        command.append("--confirm-delete")
        return command
    if operation == "update":
        if refresh_cues is not None:
            command.append("--no-dats" if refresh_cues else "--no-cuesheets")
        if systems:
            command.extend(("--systems", *systems))
        if preview:
            command.append("--dry-run")
        return command
    command.extend(("--game-dir", str(folder)))
    if operation in {"extract", "convert"}:
        command.append("--use-redump-names" if operation == "convert" and use_redump_names else "--no-rename-folder")
        if keep_sources:
            command.append("--keep-sources")
        if preview:
            command.append("--dry-run")
        if force and action and action.allows_overwrite:
            command.append("--force")
    elif operation in {"xiso", "rvz", "rvz-extract"}:
        if force:
            command.append("--force")
        if use_redump_names:
            command.append("--use-redump-names")
        if operation == "xiso":
            command.extend(("--extract-xiso", str(extract_xiso or APP_ROOT / "tools" / "extract-xiso.exe")))
        if not keep_sources:
            command.append("--delete-source")
        if preview:
            command.append("--dry-run")
    elif operation in {"manifest", "organize", "repair-layout"}:
        if preview:
            command.append("--dry-run")
    elif action and action.verification == VerificationMode.CHD:
        command.append("--chd-integrity")
    elif action and action.verification == VerificationMode.ORIGINALS:
        command.append("--rehash")
    return command


def action_for_command(command: list[str]) -> ActionSpec | None:
    if not command or command[4] == "update":
        return None
    return resolve_action(command[4], rehash="--rehash" in command, check_chds="--chd-integrity" in command)


def run_process(command: list[str], events: queue.Queue[tuple[str, str | int]]) -> None:
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        with subprocess.Popen(command, cwd=APP_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", creationflags=flags, env=env) as process:
            assert process.stdout is not None
            for line in process.stdout:
                events.put(("log", line))
            events.put(("done", process.wait()))
    except Exception as exc:
        events.put(("log", f"Unable to run operation: {exc}\n"))
        events.put(("done", 1))


class DesktopRunner:
    """Own a batch's process options, stop request, and temporary retry selection."""
    def __init__(self, events: queue.Queue[tuple[str, str | int]]):
        self.events = events
        self.last_command: list[str] = []
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.stop_file: Path | None = None

    @property
    def operation(self) -> str:
        return self.last_command[4] if self.last_command else ""

    def start(self, command: list[str], folders: list[Path] | None = None, files: list[Path] | None = None, approved_plans: dict[str, str] | None = None) -> None:
        self.cleanup()
        self.temporary = tempfile.TemporaryDirectory(prefix="redump_ui_")
        self.stop_file = Path(self.temporary.name) / "stop"
        command = list(command)
        if folders is not None or files is not None:
            for option in ("--game-dir", "--game-list", "--file-list"):
                if option in command:
                    index = command.index(option)
                    del command[index:index + 2]
            selection = Path(self.temporary.name) / "games.json"
            selection.write_text(json.dumps([str(path) for path in (files if files is not None else folders or [])]), encoding="utf-8")
            command.extend(("--file-list" if files is not None else "--game-list", str(selection)))
        if approved_plans is not None:
            approval = Path(self.temporary.name) / "approved-plans.json"
            approval.write_text(json.dumps(approved_plans), encoding="utf-8")
            command.extend(("--approved-plans", str(approval)))
        self.last_command = command
        subprocess_command = [*command, "--stop-file", str(self.stop_file)]
        threading.Thread(target=run_process, args=(subprocess_command, self.events), daemon=True).start()

    def request_stop(self) -> None:
        if self.stop_file is not None:
            self.stop_file.touch()

    def cancel_stop(self) -> None:
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)

    def cleanup(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None
        self.stop_file = None
