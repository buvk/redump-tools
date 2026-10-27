"""Action confirmation dialogs and batch-only conversion options."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .action_review import display_paths
from .actions import ActionSpec
from .qt_layout import dialog_buttons, dialog_layout, readonly_text
from .qt_widgets import label


class ReviewDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, text: str, *, files: list[Path] | None = None, accept: str = "Start",
                 action: ActionSpec | None = None, settings: dict | None = None, full_text: str | None = None):
        super().__init__(parent)
        self.option_fields: dict[str, QCheckBox] = {}
        title = title.rstrip(".…")
        self.setWindowTitle(title)
        self.resize(720, 480 if files is None else 520)
        layout = dialog_layout(self)
        layout.addWidget(label(title, heading=True))
        if files is not None:
            output = next((line.partition(": ")[2] for line in text.splitlines() if line.startswith("Output: ")), "")
            layout.addWidget(label(f"{len(files)} disc{'s' if len(files) != 1 else ''} selected" + (f" · Output: {output}" if output else ""), muted=True))
            box = QGroupBox("Options for this batch")
            form = QVBoxLayout(box)
            form.setSpacing(8)
            warnings = []
            for line in text.splitlines()[1:]:
                key, separator, value = line.partition(": ")
                if key == "Output":
                    continue
                if action and key == "Sources" or action and action.allows_overwrite and key == "Existing outputs":
                    continue
                if separator:
                    form.addWidget(label(f"{key}: {value.lower()}", muted=True))
                elif line:
                    warnings.append(line)
            if action:
                defaults = settings or {}
                options = [("keep_sources", "Keep original files", True)]
                if action.allows_overwrite:
                    options.append(("force", "Overwrite existing output files", False))
                if action.id in {"convert", "xiso", "rvz", "rvz-extract"}:
                    options.append(("use_redump_names", "Use official Redump names", False))
                descriptions = {
                    "keep_sources": "Keep the source images after processing succeeds.",
                    "force": "Overwrite existing destination files. The input image is never overwritten.",
                    "use_redump_names": "Rename the converted file and game folder when a matching DAT entry is found.",
                }
                for key, caption, default in options:
                    field = QCheckBox(caption)
                    field.setChecked(bool(defaults.get(key, default)))
                    self.option_fields[key] = field
                    form.addWidget(field)
                    description = label(descriptions[key], muted=True)
                    if key == "keep_sources":
                        def update_source_description(checked: bool, description: QLabel = description) -> None:
                            description.setText("Keep the source images after processing succeeds." if checked else
                                                "Original files will be deleted after successful processing.")
                        field.toggled.connect(update_source_description)
                        update_source_description(field.isChecked())
                    description.setContentsMargins(23, 0, 0, 4)
                    form.addWidget(description)
            layout.addWidget(box)
            for warning in warnings:
                notice = label(warning)
                notice.setProperty("warning", True)
                layout.addWidget(notice)
            file_header = QHBoxLayout()
            file_header.addWidget(label("Selected discs"))
            file_header.addStretch()
            paths = QCheckBox("Show full paths")
            file_header.addWidget(paths)
            layout.addLayout(file_header)
            viewer = QListWidget()
            viewer.setObjectName("reviewFiles")
            for path, short_name in zip(files, display_paths(files), strict=True):
                item = QListWidgetItem(short_name)
                item.setToolTip(str(path))
                viewer.addItem(item)

            def show_paths(checked: bool) -> None:
                for number, name in enumerate(display_paths(files, checked)):
                    viewer.item(number).setText(name)
            paths.toggled.connect(show_paths)
            layout.addWidget(viewer, 1)
        else:
            viewer = readonly_text(text)
            if full_text:
                paths = QCheckBox("Show full paths")
                paths.toggled.connect(lambda checked: viewer.setPlainText(full_text if checked else text))
                layout.addWidget(paths)
            layout.addWidget(viewer, 1)
        buttons = dialog_buttons(self, accept, safe_default=True)
        proceed = next(button for button in buttons.buttons() if buttons.buttonRole(button) == QDialogButtonBox.ButtonRole.AcceptRole)
        proceed.setEnabled(files is None or bool(files))
        layout.addWidget(buttons)

    def batch_options(self) -> dict[str, bool]:
        return {key: field.isChecked() for key, field in self.option_fields.items()}



def review_text(action: ActionSpec, files: list[Path], settings: dict) -> str:
    # Keep the same retention and overwrite descriptions across launchers.
    from .action_review import conversion_review
    return conversion_review(action, files, settings.get("keep_sources", True), settings.get("force", False))
