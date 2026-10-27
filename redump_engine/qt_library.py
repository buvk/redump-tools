"""Library-first Qt browser with system navigation and numeric set sorting."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QSignalBlocker,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QKeyEvent, QPainter, QPen, QPixmap, QPolygonF, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .formatting import format_size
from .library_discovery import LibrarySnapshot
from .library_groups import group_games
from .library_models import LibraryItem
from .library_presentation import selection_summary, set_sizes, set_summary
from .qt_layout import configure_data_view
from .qt_models import DISC_ROLE, INDICES_ROLE, PATH_ROLE, SORT_ROLE, LibrarySortModel
from .systems import SHORT_SYSTEM_NAMES as SYSTEM_LABELS
from .systems import SYSTEM_NAMES
from .ui_badges import verification_badge
from .ui_layout import LIBRARY_COLUMNS, library_layout

QT_LIBRARY_COLUMNS = (*LIBRARY_COLUMNS, "system", "filename")
COLUMN_INDEX = {name: index for index, name in enumerate(QT_LIBRARY_COLUMNS)}
NUMERIC_COLUMNS = frozenset({"size", "original", "saved"})



class LibraryTree(QTreeView):
    select_everything = Signal()
    selection_menu = Signal()

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex | QPersistentModelIndex) -> None:
        if self.model().hasChildren(index):
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(self.palette().windowText().color(), 1.5))
            x, y = rect.right() - self.indentation() / 2, rect.center().y()
            points = [(x - 4, y - 2), (x, y + 2), (x + 4, y - 2)] if self.isExpanded(index) else [(x - 2, y - 4), (x + 2, y), (x - 2, y + 4)]
            painter.drawPolyline(QPolygonF([QPointF(a, b) for a, b in points]))
            painter.restore()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Menu or event.key() == Qt.Key.Key_F10 and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.selection_menu.emit()
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_A:
                self.select_everything.emit()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                for column in range(self.model().columnCount()):
                    self.resizeColumnToContents(column)
                event.accept()
                return
        super().keyPressEvent(event)


class LibraryPanel(QWidget):
    selection_changed = Signal()
    details_requested = Signal()
    layout_changed = Signal()

    def __init__(self, state: dict, verification: Callable[[LibraryItem], str], colors: dict[str, str]):
        super().__init__()
        self.state = library_layout(state)
        if isinstance(state, dict) and state.get("sort_column") in {"system", "filename"}:
            self.state["sort_column"] = state["sort_column"]
        widths = state.get("columns", {}) if isinstance(state, dict) else {}
        for column in ("system", "filename"):
            width = widths.get(column) if isinstance(widths, dict) else None
            if type(width) is int and 70 <= width <= 10000:
                self.state["columns"][column] = width
        self.verification = verification
        self.colors = colors
        self.items: list[LibraryItem] = []
        self.game_groups = []
        self.hidden_columns = state.get("hidden_columns", [2]) if isinstance(state, dict) else [2]
        self.hidden_columns = [column for column in self.hidden_columns if type(column) is int and 1 <= column < len(QT_LIBRARY_COLUMNS)] if isinstance(self.hidden_columns, list) else [COLUMN_INDEX["folder"]]
        if not isinstance(widths, dict) or "filename" not in widths:
            self.hidden_columns.append(COLUMN_INDEX["filename"])
        self.shown: list[int] = []
        self.root = Path()
        self.query = self.state["query"]
        self.format_filter = self.state["filter"]
        self.grouped = self.state["group_discs"]
        self.recursive = self.state["recursive"]
        self.system = self.state["system"]
        self.groups = dict(self.state["groups"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.splitter = QSplitter()
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 6, 10, 0)
        label = QLabel("SYSTEMS")
        label.setProperty("muted", True)
        side.addWidget(label)
        self.systems = QListWidget()
        self.systems.setMinimumWidth(155)
        self.systems.currentItemChanged.connect(self._system_changed)
        side.addWidget(self.systems)
        self.splitter.addWidget(sidebar)
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Game", "Format", "Folder", "File size", "Original size", "Space saved", "Manifest", "Verification", "System", "Filename"])
        self.proxy = LibrarySortModel()
        self.proxy.setSourceModel(self.model)
        self.table = LibraryTree()
        self.table.setModel(self.proxy)
        configure_data_view(self.table)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setAllColumnsShowFocus(True)
        self.table.setIndentation(22)
        self.empty = QLabel("Open a library folder to get started.", self.table.viewport())
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setProperty("muted", True)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.table.viewport().installEventFilter(self)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.select_everything.connect(self.select_all)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self.details_requested.emit())
        self.table.expanded.connect(lambda _: self._layout_changed())
        self.table.collapsed.connect(lambda _: self._layout_changed())
        self.table.header().sectionResized.connect(lambda *_: self.layout_changed.emit())
        for index, width in enumerate((300, 80, 200, 100, 105, 150, 95, 150, 115, 300)):
            self.table.setColumnWidth(index, self.state["columns"].get(QT_LIBRARY_COLUMNS[index], width))
            self.table.setColumnHidden(index, index in self.hidden_columns)
        self.table.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.header().moveSection(self.table.header().visualIndex(COLUMN_INDEX["filename"]), 1)
        self.table.header().moveSection(self.table.header().visualIndex(COLUMN_INDEX["system"]), 2)
        self.table.sortByColumn(QT_LIBRARY_COLUMNS.index(self.state["sort_column"]),
                                Qt.SortOrder.DescendingOrder if self.state["reverse"] else Qt.SortOrder.AscendingOrder)
        self.splitter.addWidget(self.table)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([self.state["sidebar_width"], 1000])
        self.splitter.splitterMoved.connect(lambda *_: self.layout_changed.emit())
        layout.addWidget(self.splitter, 1)
        footer = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setProperty("muted", True)
        self.summary.setWordWrap(True)
        footer.addWidget(self.summary, 1)
        layout.addLayout(footer)

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self.empty.setGeometry(self.table.viewport().rect())
        return super().eventFilter(watched, event)

    def set_snapshot(self, snapshot: LibrarySnapshot, root: Path, groups=None) -> None:
        selected = {item.path for item in self.selected()}
        self.items, self.root = snapshot.items, root
        self.game_groups = groups if groups is not None else group_games(self.items)
        previous = self.system
        with QSignalBlocker(self.systems):
            self.systems.clear()
            counts = {system: sum(item.system == system and item.kind != "Manifest" for item in self.items) for system in snapshot.systems}
            entries = [("All systems", sum(counts.values())), *[(SYSTEM_NAMES.get(system or "", "Unknown system"), count) for system, count in counts.items()]]
            for name, count in entries:
                short = {"Sony - PlayStation": "PlayStation", "Sony - PlayStation 2": "PlayStation 2", "Nintendo - Nintendo Wii": "Wii",
                         "Nintendo - GameCube": "GameCube", "Sega - Dreamcast": "Dreamcast", "Microsoft - Xbox": "Xbox"}.get(name, name)
                item = QListWidgetItem(f"{short}   {count}")
                pixmap = QPixmap(22, 22)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(QColor(self.colors["muted"]), 1.6))
                painter.drawEllipse(3, 3, 16, 16)
                painter.drawEllipse(8, 8, 6, 6)
                painter.end()
                item.setIcon(QIcon(pixmap))
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.systems.addItem(item)
                if name == previous:
                    self.systems.setCurrentItem(item)
            if self.systems.currentRow() < 0:
                self.systems.setCurrentRow(0)
                self.system = "All systems"
        self.refresh_rows(selected)

    def _system_changed(self, current, previous=None) -> None:
        if current:
            self.system = current.data(Qt.ItemDataRole.UserRole)
            self.refresh_rows()

    def _remember_expanded(self) -> None:
        for row in range(self.model.rowCount()):
            item = self.model.item(row)
            key = item.data(PATH_ROLE)
            if isinstance(key, str) and key.startswith("game:"):
                self.groups[key] = self.table.isExpanded(self.proxy.mapFromSource(item.index()))

    def _layout_changed(self) -> None:
        self._remember_expanded()
        self.layout_changed.emit()

    def selected(self) -> list[LibraryItem]:
        indices = set()
        for index in self.table.selectionModel().selectedRows():
            indices.update(self.proxy.mapToSource(index).data(INDICES_ROLE) or [])
        return [self.items[index] for index in sorted(indices) if index < len(self.items)]

    def select_all(self) -> None:
        selection = QItemSelection()
        for row in range(self.model.rowCount()):
            parent = self.model.index(row, 0)
            index = self.proxy.mapFromSource(parent)
            selection.select(index, index)
            for child in range(self.model.rowCount(parent)):
                index = self.proxy.mapFromSource(self.model.index(child, 0, parent))
                selection.select(index, index)
        self.table.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)

    def restore_selection(self, paths: set[Path]) -> None:
        self.table.clearSelection()
        def visit(parent):
            for row in range(self.model.rowCount(parent)):
                index = self.model.index(row, 0, parent)
                if index.data(PATH_ROLE) in {str(path) for path in paths}:
                    self.table.selectionModel().select(self.proxy.mapFromSource(index), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
                visit(index)
        visit(QModelIndex())

    def expand_sets(self, expanded: bool) -> None:
        self.table.expandAll() if expanded else self.table.collapseAll()
        self._layout_changed()

    def set_column_visible(self, column: int, visible: bool) -> None:
        self.table.setColumnHidden(column, not visible)
        self.layout_changed.emit()

    def _selection_changed(self, *args) -> None:
        selected = self.selected()
        visible = [self.items[index] for index in self.shown]
        self.summary.setText(f"{len(visible)} discs · {len(selected)} selected    {selection_summary(selected or visible)}")
        self.selection_changed.emit()

    def _visible(self, item: LibraryItem, group_title: str) -> bool:
        if self.system != "All systems" and SYSTEM_NAMES.get(item.system or "", "Unknown system") != self.system:
            return False
        if item.kind == "Manifest":
            return False
        if self.format_filter == "Compressed" and item.kind not in {"CHD", "RVZ", "XISO"}:
            return False
        if self.format_filter == "Uncompressed" and item.kind not in {"ISO", "CUE/BIN", "BIN (no CUE)"}:
            return False
        if self.format_filter == "Missing manifest" and not item.missing_record:
            return False
        return not self.query or self.query.casefold() in (str(item.path) + item.game_title + group_title).casefold()

    def _row(self, values, numbers, indices, path: str, disc: int = 0, group: bool = False):
        row = []
        for column, value in enumerate(values):
            name = QT_LIBRARY_COLUMNS[column]
            cell = QStandardItem(str(value))
            cell.setEditable(False)
            cell.setData(numbers.get(name, str(value)), SORT_ROLE)
            if name in NUMERIC_COLUMNS:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if group:
                cell.setBackground(QBrush(QColor(self.colors["selection"])))
                font = cell.font()
                font.setWeight(QFont.Weight.DemiBold)
                cell.setFont(font)
            if name == "checked":
                color = "success" if str(value).startswith("✓") else "error" if "Failed" in str(value) else "warning" if str(value).startswith("!") else "muted"
                cell.setForeground(QBrush(QColor(self.colors[color])))
            cell.setToolTip(str(value) if group and name == "checked" else str(values[0]) if group else path)
            row.append(cell)
        row[0].setData(indices, INDICES_ROLE)
        row[0].setData(path, PATH_ROLE)
        row[0].setData(disc, DISC_ROLE)
        return row

    def refresh_rows(self, selection: set[Path] | None = None) -> None:
        selected = selection if selection is not None else {item.path for item in self.selected()}
        self._remember_expanded()
        groups = self.game_groups if self.grouped else []
        by_index = {index: group for group in groups for index in group.indices}
        self.shown = [index for index, item in enumerate(self.items) if self._visible(item, by_index[index].title if index in by_index else "")]
        with QSignalBlocker(self.table.selectionModel()):
            self.model.removeRows(0, self.model.rowCount())
            parents = {}
            for index in self.shown:
                item = self.items[index]
                group = by_index.get(index)
                folder = str(item.path.parent.relative_to(self.root)) if item.path.is_relative_to(self.root) else str(item.path.parent)
                if group and group.id not in parents:
                    members = [i for i in group.indices if i in self.shown]
                    items, numbers = [self.items[i] for i in members], [group.numbers[i] for i in members]
                    sizes = set_sizes(items, numbers)
                    totals = set_summary(items, [self.verification(member) for member in items], numbers, group.total_discs)
                    values = (group.title, f"{len(set(numbers))}/{group.total_discs} discs", folder, *totals, SYSTEM_LABELS.get(item.system or "", "Unknown"), "")
                    parent_row = self._row(values, {"size": sizes[0], "original": sizes[1], "saved": sizes[2]}, members, group.id, group=True)
                    self.model.appendRow(parent_row)
                    parents[group.id] = parent_row[0]
                saved = item.saved_percent
                saved_text = f"{saved:.1f}%" if saved is not None and saved >= 0 else f"{abs(saved):.1f}% larger" if saved is not None else "Unknown" if item.kind in {"CHD", "RVZ", "XISO"} else "—"
                values = (f"Disc {group.numbers[index]}" if group else item.game_title, item.kind, folder, format_size(item.size),
                          format_size(item.original_size) if item.original_size is not None else "Unknown",
                          saved_text, "Recorded" if not item.missing_record else "Incomplete" if item.manifest else "Missing",
                          verification_badge(self.verification(item))[0], SYSTEM_LABELS.get(item.system or "", "Unknown"), item.path.name)
                row = self._row(values, {"size": item.size, "original": item.original_size, "saved": saved}, [index], str(item.path), group.numbers[index] if group else 0)
                if group:
                    parents[group.id].appendRow(row)
                else:
                    self.model.appendRow(row)
                if item.path in selected:
                    self.table.selectionModel().select(self.proxy.mapFromSource(row[0].index()), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            for key, parent in parents.items():
                self.table.setExpanded(self.proxy.mapFromSource(parent.index()), self.groups.get(key, True))
        self._selection_changed()
        self.empty.setText("No matching discs\nTry another search or filter." if self.items else "No disc images found\nOpen a library folder to get started.")
        self.empty.setVisible(not self.shown)
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.raise_()

    def saved_layout(self) -> dict:
        self._remember_expanded()
        sizes = self.splitter.sizes()
        return {"root": str(self.root), "recursive": self.recursive, "group_discs": self.grouped, "query": self.query,
                "filter": self.format_filter, "system": self.system, "groups": dict(self.groups),
                "hidden_columns": [column for column in range(len(QT_LIBRARY_COLUMNS)) if self.table.isColumnHidden(column)],
                "columns": {column: self.table.columnWidth(i) for i, column in enumerate(QT_LIBRARY_COLUMNS)},
                "sort_column": QT_LIBRARY_COLUMNS[self.table.header().sortIndicatorSection()],
                "reverse": self.table.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder,
                "sidebar_width": sizes[0]}
