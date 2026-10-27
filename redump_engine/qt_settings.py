"""Transactional settings with appearance previews and DAT/cuesheet downloads."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .formatting import abbreviate_timezone
from .paths import CUESHEET_ROOT, DAT_ROOT
from .qt_combo import ComboBox as QComboBox
from .qt_layout import CONTROL_GAP, SECTION_GAP, dialog_buttons, dialog_layout, tab_layout
from .qt_widgets import label
from .updater import available_systems


class SettingsDialog(QDialog):
    appearance_preview = Signal(str)

    def __init__(self, parent: QWidget, settings: dict, download: Callable[[dict, bool | None], None], timestamps: dict):
        super().__init__(parent)
        self.original = dict(settings)
        self.timestamps = timestamps
        self.download = download
        self.fields: dict[str, QWidget] = {}
        self.controls: list[QWidget] = []
        self.reference_status: dict[str, tuple[QLabel, QLabel]] = {}
        self.tool_status: dict[str, QLabel] = {}
        self.setWindowTitle("Settings")
        self.resize(760, 560)
        self.setMinimumSize(660, 520)
        layout = dialog_layout(self)
        appearance_row = QHBoxLayout()
        appearance_row.addWidget(label("Appearance"))
        appearance_row.addStretch()
        appearance = QComboBox()
        appearance.addItems(["Light", "Dark"])
        appearance.setCurrentText(settings.get("appearance", "Light"))
        appearance.setFixedWidth(160)
        appearance.currentTextChanged.connect(self.appearance_preview.emit)
        self.fields["appearance"] = appearance
        appearance_row.addWidget(appearance)
        layout.addLayout(appearance_row)
        tabs = QTabWidget()
        tabs.addTab(self._references_page(), "DATs and cuesheets")
        tabs.addTab(self._tools_page(), "Tools")
        layout.addWidget(tabs, 1)
        self.controls.extend(widget for widget in self.fields.values() if widget not in self.controls)
        self.update_freshness()
        footer = dialog_buttons(self)
        layout.addWidget(footer)

    def _path_row(self, key: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        entry = QLineEdit(str(self.original.get(key, "")))
        entry.setPlaceholderText("Select an executable…")
        entry.editingFinished.connect(self.update_freshness)
        self.fields[key] = entry
        button = QPushButton("Browse…")
        button.setFixedWidth(84)
        button.clicked.connect(lambda: self.browse(key))
        layout.addWidget(entry, 1)
        layout.addWidget(button)
        self.controls.extend([entry, button])
        return row

    @staticmethod
    def _card(title: str) -> tuple[QGroupBox, QVBoxLayout]:
        card = QGroupBox(title)
        card.setProperty("settingsCard", True)
        contents = QVBoxLayout(card)
        contents.setContentsMargins(8, 10, 8, 8)
        contents.setSpacing(CONTROL_GAP)
        return card, contents

    def _references_page(self) -> QWidget:
        page = QWidget()
        layout = tab_layout(page)
        layout.setSpacing(SECTION_GAP)
        cards = QHBoxLayout()
        cards.setSpacing(SECTION_GAP)
        for key, title in (("dats", "DATs"), ("cuesheets", "Cuesheets")):
            card, contents = self._card(title)
            count, stamp = label(""), label("", muted=True)
            count.setWordWrap(False)
            stamp.setWordWrap(False)
            self.reference_status[key] = count, stamp
            contents.addWidget(count)
            contents.addWidget(stamp)
            cards.addWidget(card, 1)
        layout.addLayout(cards)
        refresh, controls = self._card("Download updates")
        row = QHBoxLayout()
        row.addWidget(label("Systems"))
        systems = QComboBox()
        systems.addItems(["All supported systems", *available_systems()])
        systems.setCurrentText(self.original.get("refresh_system", "All supported systems"))
        self.fields["refresh_system"] = systems
        row.addWidget(systems, 1)
        button = QPushButton("Refresh DATs and cuesheets")
        button.setProperty("primary", True)
        button.clicked.connect(lambda: self.download(self.values(), None))
        self.controls.append(button)
        row.addWidget(button)
        controls.addLayout(row)
        layout.addWidget(refresh)
        layout.addStretch()
        return page

    def _tools_page(self) -> QWidget:
        page = QWidget()
        layout = tab_layout(page)
        layout.setSpacing(SECTION_GAP)
        for key, title, purpose in (("chdman", "chdman", "Create, restore and verify CHD images"),
                                    ("extract_xiso", "extract-xiso", "Convert Xbox ISO images to XISO"),
                                    ("dolphin_tool", "DolphinTool", "Convert, restore and verify GameCube / Wii images")):
            card, contents = self._card(title)
            heading = QHBoxLayout()
            heading.addWidget(label(purpose, muted=True), 1)
            status = label("", muted=True)
            self.tool_status[key] = status
            heading.addWidget(status)
            contents.addLayout(heading)
            contents.addWidget(self._path_row(key))
            layout.addWidget(card)
        layout.addStretch()
        return page

    def values(self) -> dict:
        values: dict = {"dats": str(DAT_ROOT), "cuesheets": str(CUESHEET_ROOT)}
        values.update({key: self.original.get(key) for key in self.fields})
        for key, widget in self.fields.items():
            if isinstance(widget, QLineEdit):
                values[key] = widget.text().strip()
            elif isinstance(widget, QComboBox):
                values[key] = widget.currentText()
        return values

    def browse(self, key: str) -> None:
        entry = self.fields[key]
        assert isinstance(entry, QLineEdit)
        current = entry.text()
        path = QFileDialog.getOpenFileName(self, "Select tool", current)[0]
        if path:
            entry.setText(path)
            self.update_freshness()

    def update_freshness(self, *args) -> None:
        values = self.values()
        for key in ("dats", "cuesheets"):
            path = Path(values[key]).expanduser().resolve()
            stamp = self.timestamps.get(str(path))
            try:
                count = sum(file.is_file() and file.suffix.lower() in {".dat", ".xml", ".zip", ".cue"} for file in path.iterdir())
                status = f"{count} files"
            except OSError:
                status = "Folder unavailable"
            count_label, stamp_label = self.reference_status[key]
            count_label.setText(status)
            stamp_label.setText(f"Updated {abbreviate_timezone(stamp)}" if stamp else "Not downloaded yet")
        for key, status in self.tool_status.items():
            value = values[key]
            status.setText("Found" if value and Path(value).expanduser().is_file() else "Not found")

    def set_running(self, running: bool) -> None:
        for widget in self.controls:
            widget.setEnabled(not running)

    def reject(self) -> None:
        self.appearance_preview.emit(self.original.get("appearance", "Light"))
        super().reject()
