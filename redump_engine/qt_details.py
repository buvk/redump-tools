"""Reusable, copyable disc details windows."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QWidget,
)

from .qt_layout import dialog_buttons, dialog_layout, readonly_text
from .qt_widgets import bind_copy_feedback


class TextDialog(QDialog):
    def __init__(self, parent: QWidget, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 540)
        layout = dialog_layout(self)
        self.viewer = readonly_text()
        layout.addWidget(self.viewer)
        buttons = dialog_buttons(self, close_only=True)
        copy = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy.setToolTip("Copy disc details")
        reset_copy = bind_copy_feedback(copy, self.viewer.toPlainText)
        self.viewer.textChanged.connect(reset_copy)
        layout.addWidget(buttons)

    def set_text(self, text: str) -> None:
        if self.viewer.toPlainText() != text:
            self.viewer.setPlainText(text)
        self.show()
        self.raise_()
        self.activateWindow()
