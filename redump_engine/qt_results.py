"""Result table, searchable diagnostics, export, and retry controls."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .events import ResultRecord
from .qt_combo import ComboBox as QComboBox
from .qt_layout import configure_data_view, fit_columns, readonly_text, search_toolbar, tab_layout, tab_page
from .qt_models import INDICES_ROLE, SORT_ROLE
from .qt_widgets import bind_copy_feedback
from .ui_results import RESULT_HEADERS, RESULT_STATUS_COLUMN, result_columns, result_details


class ResultsPanel(QWidget):
    export_requested = Signal()
    retry_requested = Signal()
    organize_requested = Signal(object)
    folder_requested = Signal(object)

    def __init__(self, colors: dict[str, str]):
        super().__init__()
        self.colors = colors
        self.records: list[ResultRecord] = []
        self.running = False
        layout = tab_layout(self)
        self.filter = QComboBox()
        self.filter.addItems(["All results", "Failed", "Warnings", "Skipped"])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search results…")
        self.search.setClearButtonEnabled(True)
        self.export = QPushButton("Export CSV…")
        self.export.setEnabled(False)
        self.export.clicked.connect(self.export_requested.emit)
        self.retry = QPushButton("Retry failed")
        self.retry.setEnabled(False)
        self.retry.clicked.connect(self.retry_requested.emit)
        layout.addLayout(search_toolbar(self.search, self.filter, (self.retry, self.export)))
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(list(RESULT_HEADERS))
        self.model.setSortRole(SORT_ROLE)
        self.table.setModel(self.model)
        configure_data_view(self.table)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        for column, width in enumerate((320, 120, 270, 190, 450)):
            self.table.setColumnWidth(column, width)
        self.table.selectionModel().selectionChanged.connect(self.select_result)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.splitter.addWidget(self.table)
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        detail_bar = QHBoxLayout()
        detail_bar.addStretch()
        self.open_folder = QAction("Open folder", self)
        self.open_folder.triggered.connect(self._open_folder)
        self.organize = QPushButton("Fix names and folders…")
        self.organize.clicked.connect(self._organize)
        self.copy = QPushButton("Copy")
        detail_bar.addWidget(self.organize)
        detail_bar.addWidget(self.copy)
        self.details = readonly_text(placeholder="Select a result to read its details.")
        self.details.textChanged.connect(self.update_copy_state)
        self.log = readonly_text(placeholder="Operation output will appear here.", max_blocks=20000)
        self.log.textChanged.connect(self.update_copy_state)
        self.diagnostics = QTabWidget()
        for title, viewer in (("Details", self.details), ("Log", self.log)):
            self.diagnostics.addTab(tab_page(viewer), title)
        diagnostic_actions = QWidget()
        detail_bar.setContentsMargins(0, 0, 0, 0)
        diagnostic_actions.setLayout(detail_bar)
        self.diagnostics.setCornerWidget(diagnostic_actions, Qt.Corner.TopRightCorner)
        self.reset_copy_feedback = bind_copy_feedback(self.copy, lambda: self.active_text().toPlainText())
        self.diagnostics.currentChanged.connect(self.update_copy_state)
        detail_layout.addWidget(self.diagnostics)
        self.splitter.addWidget(detail_panel)
        self.splitter.setSizes([430, 220])
        layout.addWidget(self.splitter, 1)
        self.filter.currentTextChanged.connect(self.refresh_rows)
        self.search.textChanged.connect(self.refresh_rows)
        self.select_result()

    def active_text(self) -> QPlainTextEdit:
        return self.log if self.diagnostics.currentIndex() == 1 else self.details

    def update_copy_state(self, *args) -> None:
        if hasattr(self, "reset_copy_feedback"):
            self.reset_copy_feedback()
            self.copy.setToolTip("Copy log" if self.diagnostics.currentIndex() == 1 else "Copy result details")

    def fit_columns(self) -> None:
        fit_columns(self.table)

    def selected_record(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            item = self.model.item(rows[0].row(), 0)
            if item is None:
                return None
            index = item.data(INDICES_ROLE)
            if isinstance(index, int) and index < len(self.records):
                return self.records[index]
        return None

    def refresh_rows(self, *args) -> None:
        selected = self.selected_record()
        self.table.setSortingEnabled(False)
        self.model.removeRows(0, self.model.rowCount())
        query = self.search.text().casefold()
        wanted = {"Warnings": "Warning", "Failed": "Failed", "Skipped": "Skipped"}.get(self.filter.currentText())
        selection = None
        for index, record in enumerate(self.records):
            if wanted and record.status != wanted or query and query not in " ".join((*result_columns(record), record.details)).casefold():
                continue
            values = result_columns(record)
            row = [QStandardItem(value) for value in values]
            for item, value in zip(row, values, strict=True):
                item.setData(value.casefold(), SORT_ROLE)
            row[0].setData(index, INDICES_ROLE)
            for item in row:
                item.setToolTip(record.details)
            key = "error" if record.status == "Failed" else "warning" if record.status == "Warning" else "success" if record.status in {"Passed", "Completed", "Updated"} else "muted"
            row[RESULT_STATUS_COLUMN].setForeground(QColor(self.colors[key]))
            self.model.appendRow(row)
            if record is selected:
                selection = row[0]
        self.table.setSortingEnabled(True)
        if selection is not None:
            self.table.selectRow(selection.row())
        self.export.setEnabled(bool(self.records) and not self.running)
        self.retry.setEnabled(any(record.status == "Failed" and record.source for record in self.records) and not self.running)
        self.select_result()

    def select_result(self, *args) -> None:
        record = self.selected_record()
        self.details.setPlainText(result_details(record) if record else "")
        self.open_folder.setEnabled(bool(record and record.folder))
        self.organize.setVisible(bool(record and record.suggested_action in {"repair-layout", "organize"}))
        self.organize.setEnabled(not self.running)

    def context_menu(self, position):
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        self.table.selectRow(index.row())
        menu = QMenu(self)
        menu.addAction(self.open_folder)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _open_folder(self):
        record = self.selected_record()
        if record and record.folder:
            self.folder_requested.emit(record.folder)

    def _organize(self):
        record = self.selected_record()
        if record and record.source and not self.running:
            self.organize_requested.emit(record.source)

    def reset(self, heading: str) -> None:
        self.records.clear()
        self.log.clear()
        self.log.appendPlainText(heading)
        self.refresh_rows()

    def append_log(self, text: str) -> None:
        bar = self.log.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        self.log.appendPlainText(text.rstrip())
        if follow:
            bar.setValue(bar.maximum())
