"""BatchOperations: focused orchestration for the desktop window."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QMessageBox,
    QProgressDialog,
)

from .action_review import display_paths
from .actions import ACTIONS
from .layout_review import prepare_layout_review
from .qt_action_review import ReviewDialog, review_text
from .ui_results import completion_summary
from .ui_runner import action_for_command, build_command
from .verification_cache import signature
from .workflow_delete import deletion_plan

if TYPE_CHECKING:
    from .qt_app import RedumpWindow


class BatchOperations:
    def __init__(self, window: RedumpWindow):
        self.window = window

    def start_action(self, name: str, files: list[Path] | None = None) -> None:
        window = self.window
        if window.busy or window.reviewing:
            return
        files = files if files is not None else window.action_paths(name)
        if not files:
            return
        action = ACTIONS[name]
        paths = {"chdman": window.settings["chdman"], "extract-xiso": window.settings["extract_xiso"], "DolphinTool": window.settings["dolphin_tool"]}
        missing = [tool for tool in action.required_tools(files) if not Path(paths[tool]).expanduser().is_file()]
        if missing:
            QMessageBox.warning(window, "Required tools missing", "Select these tools in Settings:\n" + "\n".join(missing))
            return
        if name == "organize":
            window.prepare_organization(files)
            return
        if name == "delete":
            try:
                selected = [item for item in window.library.selected() if item.path in files]
                targets, retained = deletion_plan(selected, window.root_folder)
            except (OSError, ValueError) as error:
                QMessageBox.warning(window, "Cannot delete selection", str(error))
                return
            def deletion_text(full_paths: bool) -> str:
                text = "These files will be permanently deleted:\n\n" + "\n".join(display_paths(targets, full_paths))
                if retained:
                    text += "\n\nKept for other discs:\n" + "\n".join(display_paths(retained, full_paths))
                return text

            if not targets or ReviewDialog(window, "Delete selected files", deletion_text(False), accept="Delete permanently",
                                           full_text=deletion_text(True)).exec() != QDialog.DialogCode.Accepted:
                return
            window.begin(window.command(name), name, targets)
            return
        batch_settings = {**window.settings, "keep_sources": True, "force": False, "use_redump_names": False}
        if name in {"convert", "extract", "xiso", "rvz", "rvz-extract"}:
            review = ReviewDialog(window, action.label, review_text(action, files, batch_settings), files=files,
                                  action=action, settings=batch_settings)
            if review.exec() != QDialog.DialogCode.Accepted:
                return
            batch_settings.update(review.batch_options())
        elif name == "manifest" and any(path.suffix.lower() in {".chd", ".rvz"} for path in files):
            if ReviewDialog(window, "Create missing manifests", "Compressed images will be extracted one at a time to temporary storage and hashed.\n\n"
                            "The source images will be kept. Temporary space for the largest original disc is required.", accept="Create manifests").exec() != QDialog.DialogCode.Accepted:
                return
        window.begin(window.command(name, batch_settings), name, files)


    def command(self, name: str, settings: dict | None = None, refresh_cues: bool | None = None):
        window = self.window
        values = settings or window.settings
        system = values.get("refresh_system", "All supported systems")
        return build_command(name, window.root_folder, chdman=Path(values["chdman"]).expanduser().resolve(),
                             dats=Path(values["dats"]).expanduser().resolve(), cues=Path(values["cuesheets"]).expanduser().resolve(),
                             extract_xiso=Path(values["extract_xiso"]).expanduser().resolve(), dolphin_tool=Path(values["dolphin_tool"]).expanduser().resolve(),
                             keep_sources=values.get("keep_sources", True), force=values.get("force", False),
                             use_redump_names=values.get("use_redump_names", False), refresh_cues=refresh_cues,
                             systems=None if system == "All supported systems" else [system])


    def prepare_organization(self, files: list[Path]) -> None:
        window = self.window
        window.reviewing = True
        window.review_token += 1
        token = window.review_token
        window.review_cancel = threading.Event()
        cancelled = window.review_cancel
        dats = Path(window.settings["dats"])
        window.review_dialog = QProgressDialog("Loading Redump references…", "Cancel", 0, 0, window)
        window.review_dialog.setWindowTitle("Preparing organization review")
        window.review_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        window.review_dialog.canceled.connect(window.cancel_review)
        window.review_dialog.show()
        window.set_operation_state()

        def worker():
            try:
                review = prepare_layout_review(files, dats, cancelled, lambda progress: window.jobs.put(("review-progress", (token, progress))), organize=True)
                window.jobs.put(("review", (token, files, review, None)))
            except Exception as error:
                window.jobs.put(("review", (token, files, None, str(error))))
        threading.Thread(target=worker, daemon=True).start()


    def cancel_review(self):
        window = self.window
        if window.review_cancel:
            window.review_cancel.set()
        window.review_token += 1
        window.reviewing = False
        window.set_operation_state()


    def start_update(self, values: dict, cuesheets: bool | None = None) -> None:
        window = self.window
        if window.busy or window.reviewing:
            return
        for key in ("dats", "cuesheets"):
            if cuesheets is True and key == "dats" or cuesheets is False and key == "cuesheets":
                continue
            path = Path(values[key]).expanduser()
            if not values[key].strip() or path.exists() and not path.is_dir():
                QMessageBox.warning(window, "Download folder unavailable", f"Cannot use the {key} download folder:\n{path}")
                return
        window.begin(window.command("update", values, cuesheets), "update", None)


    def begin(self, command: list[str], operation: str, files: list[Path] | None, approved_plans: dict[str, str] | None = None) -> None:
        window = self.window
        window.busy, window.started, window.stopped = True, time.monotonic(), False
        window.counts.clear()
        window.results.running = True
        window.results.reset(f"{ACTIONS[operation].running_label if operation in ACTIONS else 'Refreshing reference files'}\n")
        window.tabs.setCurrentIndex(1)
        window.summary.hide()
        window.progress.setRange(0, 0)
        window.task_title.setText(ACTIONS[operation].running_label if operation in ACTIONS else "Refreshing reference files")
        window.stop_button.setEnabled(True)
        window.stop_pending = False
        window.stop_notice.hide()
        window.stop_button.setText("Stop after current game")
        window.stop_button.setVisible(operation != "update")
        window.task.show()
        window.set_operation_state()
        try:
            window.runner.start(command, files=files, approved_plans=approved_plans)
        except Exception as error:
            window.results.append_log(str(error))
            window.finish(1)


    def stop(self):
        window = self.window
        if not window.busy or window.stopped:
            return
        try:
            if window.stop_pending:
                window.runner.cancel_stop()
            else:
                window.runner.request_stop()
        except OSError as error:
            QMessageBox.warning(window, "Cannot change stop request", str(error))
            return
        window.stop_pending = not window.stop_pending
        window.stop_notice.setVisible(window.stop_pending)
        window.stop_button.setText("Continue batch" if window.stop_pending else "Stop after current game")


    def set_operation_state(self):
        window = self.window
        blocked = window.busy or window.reviewing
        for control in (window.folder_combo, window.open_button, window.recursive_action, window.group_action,
                        window.update_action):
            control.setEnabled(not blocked)
        window.refresh_button.setEnabled(not blocked and not window.scanning)
        window.library.systems.setEnabled(not blocked)
        if window.settings_dialog:
            window.settings_dialog.set_running(window.busy)
        window.update_action_states()


    def finish(self, exit_code: int):
        window = self.window
        window.busy = False
        window.results.running = False
        window.task.hide()
        window.stop_pending = False
        window.stop_notice.hide()
        window.stop_button.setText("Stop after current game")
        text, color, show_log = completion_summary(window.counts, window.results.records, preview=False, exit_code=exit_code,
                                                  stopped=window.stopped, elapsed=time.monotonic() - window.started)
        window.summary.setText(text)
        window.summary.show()
        if show_log:
            window.results.diagnostics.setCurrentIndex(1)
        window.runner.cleanup()
        window.set_operation_state()
        window.refresh()
        window.schedule_persist()
        if window.runner.operation in {"organize", "repair-layout", "convert", "xiso", "rvz", "rvz-extract"}:
            paths = {record.reference for record in window.results.records if record.status in {"Completed", "Warning"} and record.reference}
            if paths:
                window.recheck_manifests(paths)


    def recheck_manifests(self, paths: set[Path]) -> None:
        window = self.window
        dats = Path(window.settings["dats"])

        def worker():
            from .dat_loader import load_dat_index
            from .manifest_layout import manifest_layout_warnings
            from .metadata import load_manifest
            from .workflow_verify import VerifyWorkflow
            try:
                index = load_dat_index(dats)
            except (OSError, ValueError, KeyError, TypeError) as error:
                for path in paths:
                    window.jobs.put(("manifest-recheck-error", (path, str(error))))
                return
            workflow = VerifyWorkflow(index, False, False)
            for path in paths:
                try:
                    inputs = signature(path, ACTIONS["verify"].verification, dats)
                    if index is not None and index.source_inputs is not None and inputs and inputs["dats"] != list(index.source_inputs):
                        raise ValueError("DAT files changed after loading; run the check again")
                    result = workflow.process_game_dir(path.parent, path)
                    status = result.status.capitalize()
                    manifest = load_manifest(path)
                    if status == "Passed" and manifest and manifest_layout_warnings(path, manifest, index):
                        status = "Warning"
                    if inputs is None or inputs != signature(path, ACTIONS["verify"].verification, dats):
                        raise ValueError("Manifest or DAT changed during verification; run the check again")
                    window.jobs.put(("repair-verification", (path, dats, status, inputs)))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    window.jobs.put(("manifest-recheck-error", (path, str(error))))
        threading.Thread(target=worker, daemon=True).start()


    def retry_failed(self):
        window = self.window
        files = list(dict.fromkeys(record.source for record in window.results.records if record.status == "Failed" and record.source and record.source.is_file()))
        if files and not window.busy:
            action = action_for_command(window.runner.last_command)
            if action:
                window.start_action(action.id, files)
