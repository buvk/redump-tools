"""BackgroundEvents: focused orchestration for the desktop window."""
from __future__ import annotations

import queue
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QMessageBox,
)

from .actions import VerificationMode
from .events import PREFIX, ResultRecord, decode_event
from .formatting import abbreviate_timezone
from .qt_action_review import ReviewDialog
from .systems import system_from_path
from .ui_runner import action_for_command

if TYPE_CHECKING:
    from .qt_app import RedumpWindow


class BackgroundEvents:
    def __init__(self, window: RedumpWindow):
        self.window = window

    def poll(self):
        window = self.window
        if window.busy:
            window.elapsed.setText(f"{time.monotonic() - window.started:.0f}s elapsed")
        library_dirty = results_dirty = False
        for _ in range(200):
            try:
                kind, value = window.events.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                window.finish(int(value))
                results_dirty = True
                continue
            event = decode_event(str(value))
            if event is None:
                if not str(value).startswith(PREFIX):
                    window.results.append_log(str(value))
                continue
            if event.kind == "result" and event.result:
                result = replace(event.result, operation=event.result.operation or window.runner.operation or None)
                window.results.records.append(result)
                results_dirty = True
                action = action_for_command(window.runner.last_command)
                if action and action.verification != VerificationMode.NONE and result.source:
                    window.cache.remember_result(result, action.verification, Path(window.settings["dats"]))
                elif window.runner.operation != "update":
                    window.refresh()
                if result.status == "Updated" and result.reference:
                    window.download_times[str(result.reference.parent)] = abbreviate_timezone(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
                    window.schedule_persist()
                    if window.settings_dialog:
                        window.settings_dialog.update_freshness()
            elif event.kind == "progress":
                window.progress.setRange(0, event.total)
                window.progress.setValue(event.current - 1)
                item_name = event.folder.name if event.folder else ""
                if event.folder and item_name.lower() == "manifest.json":
                    item_name = event.folder.parent.name
                elif item_name.lower().endswith(".manifest.json"):
                    item_name = item_name[:-len(".manifest.json")]
                window.task_title.setText(f"{event.current} / {event.total} · {item_name}")
            elif event.kind == "tool-progress" and event.tool:
                tool = event.tool
                if tool.percent is not None:
                    window.progress.setRange(0, 1000)
                    window.progress.setValue(round(tool.percent * 10))
                    window.task_title.setText(f"{tool.source.name if tool.source else tool.operation} · {tool.percent:.1f}%")
                else:
                    window.progress.setRange(0, 0)
                    window.task_title.setText(tool.message or tool.operation)
            elif event.kind == "summary":
                window.counts = event.counts
            elif event.kind == "stopped":
                window.stopped = True
                window.stop_pending = False
                window.stop_notice.hide()
                window.stop_button.setEnabled(False)
            elif event.kind == "paths-renamed":
                selected = window.pending_selection or {item.path for item in window.library.selected()}
                window.pending_selection = {event.path_changes.get(path, path) for path in selected}
            elif event.kind == "folder-renamed" and event.previous_folder and event.folder:
                selected = window.pending_selection or {item.path for item in window.library.selected()}
                window.pending_selection = {event.folder / path.relative_to(event.previous_folder) if path.is_relative_to(event.previous_folder) else path for path in selected}
        for _ in range(20):
            try:
                kind, payload = window.jobs.get_nowait()
            except queue.Empty:
                break
            if kind == "scan":
                generation, folder, snapshot, groups, eligible, error = payload
                if generation != window.generation:
                    continue
                window.scanning = False
                if snapshot:
                    window.eligibility = eligible
                    window.library.set_snapshot(snapshot, folder, groups)
                    if window.pending_selection is not None:
                        window.library.restore_selection(window.pending_selection)
                        window.pending_selection = None
                    for path in {path for item in snapshot.items for path in (item.path, item.manifest) if path is not None}:
                        if str(path) in window.cache.records:
                            window.cache.request(path, Path(window.settings["dats"]))
                else:
                    window.summary.setText(f"Could not scan library: {error}")
                    window.summary.show()
                window.set_operation_state()
                if window.scan_pending:
                    window.scan_pending = False
                    window.refresh()
            elif kind == "manifest-recheck-error":
                path, error = payload
                detail = f"Automatic manifest check could not complete: {error}"
                window.results.append_log(f"{path}: {detail}")
                window.results.records.append(ResultRecord(path.parent.name, "Warning", detail, path.parent, reference=path, source=path, system=system_from_path(path), operation="verify"))
                window.cache.labels.pop(path, None)
                if window.cache.records.pop(str(path), None) is not None:
                    window.cache_dirty = True
                    window.cache_timer.start(350)
                results_dirty = library_dirty = True
            elif kind == "repair-verification":
                path, dats, status, inputs = payload
                window.cache.request(path, dats, VerificationMode.MANIFEST, f"Manifest: {status}", checked_inputs=inputs)
            elif kind == "review-progress":
                token, progress = payload
                if token == window.review_token and window.review_dialog:
                    window.review_dialog.setRange(0, 0)
                    window.review_dialog.setLabelText(f"Checking {progress.current} / {progress.total} · {progress.path.name}")
            elif kind == "review":
                token, files, review, error = payload
                if token != window.review_token:
                    continue
                window.reviewing = False
                if window.review_dialog:
                    window.review_dialog.reset()
                    window.review_dialog.deleteLater()
                    window.review_dialog = None
                window.set_operation_state()
                if error:
                    QMessageBox.warning(window, "Cannot organize selection", error)
                elif review is not None and review.planned:
                    separator = "\n\n" + "─" * 48 + "\n\n"
                    full_text = separator.join([*review.full_descriptions, *(f"Will not change:\n{record.source}\n{record.details}" for record in review.failures)])
                    if ReviewDialog(window, "Review organization changes", separator.join(review.display), accept="Apply changes", full_text=full_text).exec() == QDialog.DialogCode.Accepted:
                        window.begin(window.command("organize"), "organize", [Path(path) for path in review.approved_plans], review.approved_plans)
                elif review is not None and not review.failures:
                    QMessageBox.information(window, "No changes needed", "The selected games already have the correct names and folders.")
                elif review is not None:
                    QMessageBox.warning(window, "Cannot organize selection", "\n\n".join(review.display[:10]))
        changed, saved = window.cache.drain()
        library_dirty |= changed
        if saved:
            window.cache_dirty = True
            window.cache_timer.start(350)
        if library_dirty:
            window.library.refresh_rows()
        if results_dirty:
            window.results.refresh_rows()
