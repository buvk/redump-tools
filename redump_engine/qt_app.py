"""Qt desktop shell; disc workflows continue to run in the existing CLI process."""
from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .actions import ACTIONS
from .library_discovery import scan_library_snapshot
from .library_groups import group_games
from .library_presentation import disc_details
from .paths import APP_ROOT, CUESHEET_ROOT, DAT_ROOT, ICON_PATH, SETTINGS_PATH
from .qt_background import BackgroundEvents
from .qt_combo import ComboBox as QComboBox
from .qt_details import TextDialog
from .qt_layout import configure_tab_layout, fit_columns, search_toolbar
from .qt_library import LibraryPanel
from .qt_menus import WindowMenus
from .qt_operations import BatchOperations
from .qt_results import ResultsPanel
from .qt_settings import SettingsDialog
from .qt_theme import apply_theme
from .qt_widgets import label
from .ui_layout import LIBRARY_FILTERS
from .ui_results import export_results_csv
from .ui_runner import DesktopRunner
from .ui_settings import load_last_folder, load_preferences, load_recent_folders, save_last_folder, save_preferences
from .verification_cache import VerificationCache
from .verification_storage import load_verification_records, save_verification_records, verification_cache_path


class RedumpWindow(QMainWindow):
    recent_menu: QMenu
    action_menu: QMenu
    disc_details_action: QAction
    open_manifest_action: QAction
    open_folder_action: QAction
    select_all_action: QAction
    group_action: QAction
    recursive_action: QAction
    update_action: QAction

    def __init__(self, *, settings_path: Path = SETTINGS_PATH, autoload: bool = True):
        super().__init__()
        self.settings_path = settings_path
        self.settings = load_preferences(settings_path)
        self.settings.pop("show_log", None)
        self.settings.update(dats=str(DAT_ROOT), cuesheets=str(CUESHEET_ROOT))
        defaults = {"chdman": APP_ROOT / "tools/chdman.exe", "extract_xiso": APP_ROOT / "tools/extract-xiso.exe",
                    "dolphin_tool": APP_ROOT / "tools/DolphinTool.exe", "dats": DAT_ROOT, "cuesheets": CUESHEET_ROOT}
        for key, value in defaults.items():
            if not isinstance(self.settings.get(key), str) or not self.settings[key].strip():
                self.settings[key] = str(value)
        for key, value in {"keep_sources": True, "force": False, "use_redump_names": False,
                           "refresh_system": "All supported systems", "appearance": "Light"}.items():
            self.settings.setdefault(key, value)
        self.settings["appearance"] = "Dark" if self.settings["appearance"] == "Dark" else "Light"
        self.colors = apply_theme(cast(QApplication, QApplication.instance()), self.settings["appearance"])
        self.current_appearance = self.settings["appearance"]
        self.setWindowTitle("Redump Tools")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        window_state = self.settings.get("window_layout", {})
        window_state = window_state if isinstance(window_state, dict) else {}
        width, height = window_state.get("width", 1320), window_state.get("height", 840)
        self.resize(max(1000, width if type(width) is int else 1320), max(680, height if type(height) is int else 840))
        self.setMinimumSize(1000, 680)
        self.busy = False
        self.reviewing = False
        self.scanning = False
        self.scan_pending = False
        self.generation = 0
        self.started = 0.0
        self.counts: dict[str, int] = {}
        self.stopped = False
        self.stop_pending = False
        self.eligibility: dict[tuple[Path, str], bool] = {}
        self.pending_selection: set[Path] | None = None
        self.events: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.jobs: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.runner = DesktopRunner(self.events)
        self.cache_path = verification_cache_path(settings_path)
        self.cache = VerificationCache(load_verification_records(self.cache_path))
        self.cache_dirty = False
        self.dialogs: dict[str, TextDialog] = {}
        self.settings_dialog: SettingsDialog | None = None
        self.review_cancel: threading.Event | None = None
        self.review_token = 0
        self.review_dialog: QProgressDialog | None = None
        self.download_times = self.settings.get("reference_downloads", {})
        if not isinstance(self.download_times, dict):
            self.download_times = {}
        self.download_times = {key: value for key, value in self.download_times.items() if isinstance(key, str) and isinstance(value, str)}
        self.persist_timer = QTimer(self)
        self.persist_timer.setSingleShot(True)
        self.persist_timer.timeout.connect(self.persist)
        self.cache_timer = QTimer(self)
        self.cache_timer.setSingleShot(True)
        self.cache_timer.timeout.connect(self.persist_cache)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(12)
        self.setCentralWidget(content)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.setMinimumWidth(260)
        self.folder_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.folder_combo.addItems(load_recent_folders(settings_path))
        self.folder_combo.setCurrentText(str(load_last_folder(settings_path)))
        self.folder_combo.setToolTip("Library folder")
        self.folder_combo.activated.connect(lambda _: self.choose_path(self.folder_combo.currentText()))
        folder_entry = self.folder_combo.lineEdit()
        assert folder_entry is not None
        folder_entry.editingFinished.connect(self.folder_edited)
        self.open_button = QPushButton("Change library folder…")
        self.open_button.clicked.connect(self.choose_folder)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search this library…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(150)
        self.filter = QComboBox()
        self.filter.addItems(LIBRARY_FILTERS)
        self.filter.setMinimumWidth(150)
        location_label = QLabel("Library folder")
        location_label.setProperty("muted", True)
        toolbar.addWidget(location_label)
        toolbar.addWidget(self.folder_combo, 1)
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.tabs = QTabWidget()
        self.library = LibraryPanel(self.settings.get("library_layout", {}), self.verification, self.colors)
        browsing = search_toolbar(self.search, self.filter)
        library_layout = cast(QVBoxLayout, self.library.layout())
        configure_tab_layout(library_layout)
        library_layout.insertLayout(0, browsing)
        self.library.details_requested.connect(self.show_disc_details)
        self.library.layout_changed.connect(self.schedule_persist)
        self.library.selection_changed.connect(self.update_action_states)
        self.library.table.customContextMenuRequested.connect(self.context_menu)
        self.library.table.selection_menu.connect(self.keyboard_menu)
        self.results = ResultsPanel(self.colors)
        self.results.export_requested.connect(self.export_csv)
        self.results.retry_requested.connect(self.retry_failed)
        self.results.organize_requested.connect(lambda path: self.start_action("organize", [path]))
        self.results.folder_requested.connect(self.open_path)
        self.tabs.addTab(self.library, "Library")
        self.tabs.addTab(self.results, "Results")
        layout.addWidget(self.tabs, 1)
        self.search.setText(self.library.query)
        self.filter.setCurrentText(self.library.format_filter)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.filter_library)
        self.search.textChanged.connect(lambda _: self.search_timer.start(120))
        self.filter.currentTextChanged.connect(self.filter_library)
        self.task = QWidget()
        task_layout = QVBoxLayout(self.task)
        task_layout.setContentsMargins(0, 6, 0, 0)
        task_header = QHBoxLayout()
        self.task_title = QLabel()
        self.elapsed = label("", muted=True)
        self.elapsed.setWordWrap(False)
        self.stop_notice = label("Stop requested", muted=True)
        self.stop_notice.setWordWrap(False)
        self.stop_notice.hide()
        self.stop_button = QPushButton("Stop after current game")
        self.stop_button.clicked.connect(self.stop)
        task_header.addWidget(self.task_title, 1)
        task_header.addWidget(self.elapsed)
        task_header.addWidget(self.stop_notice)
        task_header.addWidget(self.stop_button)
        task_layout.addLayout(task_header)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        task_layout.addWidget(self.progress)
        layout.addWidget(self.task)
        self.task.hide()
        self.summary = label("", muted=True)
        self.summary.hide()
        layout.addWidget(self.summary)
        self.operation_actions: dict[str, QAction] = {}
        self.menus = WindowMenus(self)
        self.operations = BatchOperations(self)
        self.background = BackgroundEvents(self)
        self.build_menus()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(75)
        self.set_operation_state()
        if autoload:
            QTimer.singleShot(0, self.refresh)

    @property
    def root_folder(self) -> Path:
        return Path(self.folder_combo.currentText()).expanduser().resolve()

    def add_action(self, menu: QMenu, text: str, callback, shortcut: str | None = None) -> QAction:
        return self.menus.add_action(menu, text, callback, shortcut)

    def build_menus(self) -> None:
        return self.menus.build_menus()

    def update_recent(self) -> None:
        return self.menus.update_recent()

    def choose_folder(self) -> None:
        if self.busy or self.reviewing:
            return
        folder = QFileDialog.getExistingDirectory(self, "Open library folder", str(self.root_folder))
        if folder:
            self.choose_path(folder)

    def choose_path(self, folder: str) -> None:
        if self.busy or self.reviewing:
            return
        self.folder_combo.setCurrentText(folder)
        if Path(folder).is_dir():
            save_last_folder(Path(folder), self.settings_path)
            self.library.system = "All systems"
            self.refresh()

    def folder_edited(self) -> None:
        if self.root_folder != self.library.root and self.root_folder.is_dir():
            self.choose_path(str(self.root_folder))

    def filter_library(self, *args) -> None:
        self.library.query = self.search.text()
        self.library.format_filter = self.filter.currentText()
        self.library.refresh_rows()
        self.schedule_persist()

    def set_grouped(self, checked: bool) -> None:
        self.library.grouped = checked
        self.library.refresh_rows()
        self.schedule_persist()

    def set_recursive(self, checked: bool) -> None:
        self.library.recursive = checked
        self.refresh()

    def fit_columns(self) -> None:
        if self.tabs.currentWidget() is self.results:
            self.results.fit_columns()
        else:
            fit_columns(self.library.table)
            self.schedule_persist()

    def refresh_library_tab(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.refresh()

    def refresh(self) -> None:
        folder = self.root_folder
        if not folder.is_dir():
            self.summary.setText("Open an existing library folder to get started.")
            self.summary.show()
            return
        if self.scanning:
            self.scan_pending = True
            return
        self.generation += 1
        generation = self.generation
        self.scanning = True
        self.refresh_button.setEnabled(False)
        recursive = self.library.recursive
        self.set_operation_state()

        def worker():
            try:
                snapshot = scan_library_snapshot(folder, recursive)
                groups = group_games(snapshot.items)
                eligible = {(item.path, name): item.eligible(name) for item in snapshot.items for name in self.operation_actions}
                self.jobs.put(("scan", (generation, folder, snapshot, groups, eligible, None)))
            except Exception as error:
                self.jobs.put(("scan", (generation, folder, None, None, None, str(error))))
        threading.Thread(target=worker, daemon=True).start()

    def verification(self, item) -> str:
        own = self.cache.labels.get(item.path, "")
        if own.endswith(": Failed"):
            return own
        shared = self.cache.labels.get(item.manifest, "") if item.manifest else ""
        if shared.endswith((": Failed", ": Warning")):
            return shared.replace("Manifest:", "Original manifest:") if item.kind == "XISO" else shared
        for path in (item.path, item.manifest):
            if path is not None and path in self.cache.labels:
                value = self.cache.labels[path]
                return value.replace("Manifest:", "Original manifest:") if item.kind == "XISO" else value
        return "Not checked"

    def action_paths(self, name: str) -> list[Path]:
        if self.busy or self.reviewing or self.scanning:
            return []
        action = ACTIONS[name]
        chosen = [item for item in self.library.selected() if self.eligibility.get((item.path, name), False)]
        if action.targets_manifest:
            return list(dict.fromkeys(item.manifest for item in chosen if item.manifest is not None))
        if name == "organize":
            metadata = {item.manifest for item in chosen if item.kind != "Manifest"}
            chosen = [item for item in chosen if item.kind != "Manifest" or item.path not in metadata]
        if name == "manifest":
            chosen.sort(key=lambda item: item.kind == "CHD")
        return list(dict.fromkeys(item.path for item in chosen))

    def update_action_states(self) -> None:
        for name, action in self.operation_actions.items():
            count = len(self.action_paths(name))
            action.setText(f"{ACTIONS[name].label} ({count})")
            action.setEnabled(bool(count))
            action.setToolTip("Select eligible discs" if not count else ACTIONS[name].label)
        selected = self.library.selected()
        if hasattr(self, "open_manifest_action"):
            self.open_manifest_action.setEnabled(not self.busy and len({item.manifest for item in selected if item.manifest}) == 1)
            self.open_folder_action.setEnabled(len({item.path.parent for item in selected}) == 1)
            self.disc_details_action.setEnabled(bool(selected))
            self.select_all_action.setEnabled(bool(self.library.shown))

    def context_menu(self, point) -> None:
        index = self.library.table.indexAt(point)
        if index.isValid() and not self.library.table.selectionModel().isSelected(index):
            self.library.table.setCurrentIndex(index)
        elif not index.isValid():
            self.library.table.clearSelection()
        self.update_action_states()
        self.action_menu.exec(self.library.table.viewport().mapToGlobal(point))

    def keyboard_menu(self):
        self.update_action_states()
        rect = self.library.table.visualRect(self.library.table.currentIndex())
        self.action_menu.exec(self.library.table.viewport().mapToGlobal(rect.center()))

    def start_action(self, name: str, files: list[Path] | None = None) -> None:
        return self.operations.start_action(name, files)

    def command(self, name: str, settings: dict | None = None, refresh_cues: bool | None = None):
        return self.operations.command(name, settings, refresh_cues)

    def prepare_organization(self, files: list[Path]) -> None:
        return self.operations.prepare_organization(files)

    def cancel_review(self):
        return self.operations.cancel_review()

    def start_update(self, values: dict, cuesheets: bool | None = None) -> None:
        return self.operations.start_update(values, cuesheets)

    def begin(self, command: list[str], operation: str, files: list[Path] | None, approved_plans: dict[str, str] | None = None) -> None:
        return self.operations.begin(command, operation, files, approved_plans)

    def stop(self):
        return self.operations.stop()

    def set_operation_state(self):
        return self.operations.set_operation_state()

    def poll(self):
        return self.background.poll()

    def finish(self, exit_code: int):
        return self.operations.finish(exit_code)

    def recheck_manifests(self, paths: set[Path]) -> None:
        return self.operations.recheck_manifests(paths)

    def retry_failed(self):
        return self.operations.retry_failed()

    def show_text(self, title: str, text: str):
        dialog = self.dialogs.get(title)
        if dialog is None:
            dialog = self.dialogs[title] = TextDialog(self, title)
        dialog.set_text(text)

    def show_disc_details(self):
        items = self.library.selected()
        if items:
            self.show_text("Disc details", "\n\n".join(disc_details(item, self.verification(item)) for item in items))

    def open_manifest(self):
        files = {item.manifest for item in self.library.selected() if item.manifest}
        if len(files) == 1:
            path = next(iter(files))
            try:
                self.show_text(f"Manifest — {path}", path.read_text(encoding="utf-8-sig"))
            except OSError as error:
                QMessageBox.warning(self, "Cannot open manifest", str(error))

    def open_game_folder(self):
        folders = {item.path.parent for item in self.library.selected()}
        if len(folders) == 1:
            self.open_path(next(iter(folders)))

    def open_path(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_settings(self):
        if self.settings_dialog:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        dialog = self.settings_dialog = SettingsDialog(self, self.settings, self.start_update, self.download_times)
        dialog.appearance_preview.connect(self.preview_theme)
        dialog.set_running(self.busy)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            try:
                save_preferences(values, self.settings_path)
            except OSError as error:
                QMessageBox.warning(dialog, "Settings not saved", str(error))
                continue
            self.settings = values
            self.preview_theme(values["appearance"])
            self.refresh()
            break
        self.settings_dialog = None
        dialog.deleteLater()

    def preview_theme(self, appearance: str):
        app = cast(QApplication, QApplication.instance())
        if appearance == self.current_appearance and app.property("redumpAppearance") == appearance:
            return
        self.colors = apply_theme(app, appearance)
        self.current_appearance = appearance
        self.library.colors = self.results.colors = self.colors
        self.library.refresh_rows()
        self.results.refresh_rows()

    def export_csv(self):
        if not self.results.records or self.busy:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export results", "verification-results.csv", "CSV files (*.csv)")
        if path:
            try:
                export_results_csv(Path(path), self.results.records)
            except OSError as error:
                QMessageBox.warning(self, "Cannot export results", str(error))

    def schedule_persist(self, *args):
        self.persist_timer.start(350)

    def persist(self) -> bool:
        values = {**self.settings, "library_layout": self.library.saved_layout(),
                  "reference_downloads": self.download_times,
                  "window_layout": {"width": self.normalGeometry().width(), "height": self.normalGeometry().height(), "maximized": self.isMaximized()}}
        try:
            save_preferences(values, self.settings_path)
            return True
        except OSError as error:
            self.summary.setText(f"Could not save preferences: {error}")
            self.summary.show()
            return False

    def persist_cache(self) -> bool:
        if not self.cache_dirty:
            return True
        try:
            save_verification_records(self.cache.records, self.cache_path)
            self.cache_dirty = False
            return True
        except OSError as error:
            self.summary.setText(f"Could not save verification history: {error}")
            self.summary.show()
            return False

    def closeEvent(self, event: QCloseEvent):
        if self.busy:
            QMessageBox.information(self, "Operation running", "Let the current operation finish before closing to protect files being written.")
            event.ignore()
            return
        if self.reviewing:
            self.cancel_review()
        _, saved = self.cache.drain()
        self.cache_dirty |= saved
        if self.cache.saving:
            QTimer.singleShot(50, self.close)
            event.ignore()
            return
        if not self.persist() or not self.persist_cache():
            event.ignore()
            return
        self.timer.stop()
        self.persist_timer.stop()
        self.cache_timer.stop()
        self.cache.close()
        self.runner.cleanup()
        event.accept()


def run() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Redump Tools")
    app.setOrganizationName("redump-tools")
    window = RedumpWindow()
    window.showMaximized() if window.settings.get("window_layout", {}).get("maximized") else window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
