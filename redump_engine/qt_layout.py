"""Shared spacing, dialog footers, search rows, and table sizing."""
from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLayout,
    QPlainTextEdit,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

CONTROL_GAP = 10
SECTION_GAP = 12
TAB_MARGINS = (0, CONTROL_GAP, 0, 0)
DIALOG_MARGINS = (20, 16, 20, 16)


def dialog_layout(dialog: QDialog) -> QVBoxLayout:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(*DIALOG_MARGINS)
    layout.setSpacing(SECTION_GAP)
    return layout


def configure_tab_layout(layout: QLayout) -> None:
    layout.setContentsMargins(*TAB_MARGINS)
    layout.setSpacing(CONTROL_GAP)


def tab_layout(page: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    configure_tab_layout(layout)
    return layout


def tab_page(content: QWidget) -> QWidget:
    page = QWidget()
    tab_layout(page).addWidget(content)
    return page


def search_toolbar(search: QWidget, filter_widget: QWidget, actions: Iterable[QWidget] = ()) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(CONTROL_GAP)
    row.addWidget(search, 1)
    row.addWidget(filter_widget)
    for action in actions:
        row.addWidget(action)
    return row


def dialog_buttons(dialog: QDialog, primary_text: str | None = None, *, close_only: bool = False,
                   safe_default: bool = False) -> QDialogButtonBox:
    if close_only:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    else:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        primary = (buttons.addButton(primary_text, QDialogButtonBox.ButtonRole.AcceptRole) if primary_text is not None
                   else buttons.addButton(QDialogButtonBox.StandardButton.Ok))
        primary.setProperty("primary", True)
        primary.setAutoDefault(not safe_default)
        if safe_default:
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


def readonly_text(text: str = "", *, placeholder: str = "", max_blocks: int = 0) -> QPlainTextEdit:
    viewer = QPlainTextEdit(text)
    viewer.setReadOnly(True)
    viewer.setPlaceholderText(placeholder)
    if max_blocks:
        viewer.setMaximumBlockCount(max_blocks)
    return viewer


def fit_columns(view: QTableView | QTreeView) -> None:
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    header.setStretchLastSection(False)
    try:
        if isinstance(view, QTableView):
            view.resizeColumnsToContents()
        else:
            model = view.model()
            if model is not None:
                for column in range(model.columnCount()):
                    view.resizeColumnToContents(column)
    finally:
        header.setStretchLastSection(True)


def configure_data_view(view: QTableView | QTreeView) -> None:
    """Use the same row and header presentation for library and result views."""
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(view.SelectionBehavior.SelectRows)
    view.setEditTriggers(view.EditTrigger.NoEditTriggers)
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setStretchLastSection(True)
    if isinstance(view, QTableView):
        view.setShowGrid(False)
