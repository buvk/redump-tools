"""Small shared Qt labels and clipboard feedback."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
)


def label(text: str, *, muted: bool = False, heading: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setProperty("muted", muted)
    widget.setProperty("heading", heading)
    return widget



def bind_copy_feedback(button: QPushButton, text: Callable[[], str]) -> Callable[[], None]:
    original = button.text()
    button.setMinimumWidth(max(button.sizeHint().width(), button.fontMetrics().horizontalAdvance("Copied") + 28))
    timer = QTimer(button)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: button.setText(original))

    def copy() -> None:
        value = text()
        if not value:
            return
        QApplication.clipboard().setText(value)
        button.setText("Copied")
        timer.start(2000)

    def reset() -> None:
        timer.stop()
        button.setText(original)
        button.setEnabled(bool(text()))

    button.clicked.connect(copy)
    reset()
    return reset
