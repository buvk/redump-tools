"""Dropdown fields with a consistent gap before their popup list."""
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QComboBox


class ComboBox(QComboBox):
    def showPopup(self) -> None:
        super().showPopup()
        popup = self.view().window()
        bounds = self.screen().availableGeometry()
        bottom = self.mapToGlobal(QPoint(0, self.height())).y()
        top = self.mapToGlobal(QPoint(0, 0)).y()
        if bottom + 3 + popup.height() <= bounds.bottom() + 1:
            y = bottom + 3
        elif top - 3 - popup.height() >= bounds.top():
            y = top - 3 - popup.height()
        else:
            return  # Keep Qt's screen-constrained position for unusually tall lists.
        popup.move(popup.x(), y)
