"""Shared Qt palette and restrained desktop styling."""
from pathlib import Path
from string import Template

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


def theme_colors(appearance: str) -> dict[str, str]:
    dark = appearance == "Dark"
    return ({"background": "#141820", "surface": "#1c222c", "alternate": "#202733", "text": "#e8edf5",
               "muted": "#a0adbf", "border": "#303a48", "accent": "#396ed2", "selection": "#293e61",
               "success": "#78d5a4", "warning": "#eac078", "error": "#fa9898"} if dark else
              {"background": "#f4f6f9", "surface": "#ffffff", "alternate": "#f8fafd", "text": "#253247",
               "muted": "#5f6f85", "border": "#dce3ec", "accent": "#3067d8", "selection": "#e4edff",
               "success": "#22744c", "warning": "#90621d", "error": "#b63d43"})


def apply_theme(app: QApplication, appearance: str) -> dict[str, str]:
    colors = theme_colors(appearance)
    if app.style().objectName().casefold() != "fusion":
        app.setStyle("Fusion")
    font = QFont("Segoe UI", 10)
    if app.font() != font:
        app.setFont(font)
    palette = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "background"), (QPalette.ColorRole.WindowText, "text"),
                      (QPalette.ColorRole.Base, "surface"), (QPalette.ColorRole.AlternateBase, "alternate"),
                      (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "surface"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "selection"),
                      (QPalette.ColorRole.HighlightedText, "text"), (QPalette.ColorRole.ToolTipBase, "surface"),
                      (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.Link, "accent")):
        palette.setColor(role, QColor(colors[key]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["muted"]))
    app.setPalette(palette)
    chevron = (Path(__file__).parent / "assets" / f"chevron-{'dark' if appearance == 'Dark' else 'light'}.png").as_posix()
    app.setStyleSheet(Template("""
        QMainWindow, QDialog { background: ${background}; }
        QWidget { color: ${text}; font-family: "Segoe UI"; font-size: 10pt; }
        QMenuBar { background: ${background}; padding: 3px 8px; border-bottom: 1px solid ${border}; }
        QMenuBar::item { padding: 5px 10px; border-radius: 4px; }
        QMenuBar::item:selected, QMenu::item:selected { background: ${selection}; }
        QMenu { background: ${surface}; border: 1px solid ${border}; padding: 6px; }
        QMenu::item { padding: 7px 28px 7px 12px; border-radius: 4px; }
        QMenu::item:disabled { color: ${muted}; }
        QMenu::separator { height: 1px; background: ${border}; margin: 5px 8px; }
        QLineEdit, QComboBox { background: ${surface}; border: 1px solid ${border}; border-radius: 6px; padding: 7px 10px; selection-background-color: ${selection}; }
        QLineEdit:focus, QComboBox:focus { border-color: ${accent}; }
        QComboBox { padding-right: 34px; combobox-popup: 0; }
        QComboBox:hover { border-color: ${muted}; }
        QComboBox:focus, QComboBox:on { border-color: ${accent}; }
        QComboBox::drop-down { subcontrol-origin: border; subcontrol-position: top right; width: 30px; border: none; border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
        QComboBox::down-arrow { image: url("${chevron}"); width: 16px; height: 16px; }
        QComboBox QAbstractItemView { background: ${surface}; color: ${text}; border: 1px solid ${border}; border-radius: 6px; selection-background-color: ${selection}; outline: 0; padding: 6px; }
        QComboBox QAbstractItemView::item { min-height: 28px; padding: 3px 8px; border-radius: 4px; }
        QComboBox QAbstractItemView::item:selected, QComboBox QAbstractItemView::item:hover { background: ${selection}; color: ${text}; }
        QPushButton, QToolButton { background: ${surface}; border: 1px solid ${border}; border-radius: 6px; padding: 7px 13px; }
        QPushButton:hover, QToolButton:hover { background: ${selection}; }
        QPushButton:disabled, QToolButton:disabled { color: ${muted}; }
        QPushButton[primary="true"] { background: ${accent}; color: white; border-color: ${accent}; }
        QTreeView, QTableView, QListWidget { background: ${surface}; alternate-background-color: ${alternate}; border: 1px solid ${border}; border-radius: 6px; outline: 0; }
        QTreeView::item, QTableView::item { padding: 8px 6px; border: none; }
        QTreeView::item:selected, QTableView::item:selected, QListWidget::item:selected { background: ${selection}; color: ${text}; }
        QTreeView::branch { background: transparent; }
        QListWidget { background: ${background}; border: none; }
        QListWidget::item { padding: 11px 12px; margin: 2px 0; border-radius: 6px; }
        QHeaderView { background: ${surface}; }
        QTableView { gridline-color: ${border}; }
        QHeaderView::section { background: ${surface}; color: ${muted}; border: none; border-bottom: 1px solid ${border}; padding: 9px 8px; font-weight: 600; }
        QTabWidget::pane { border: none; }
        QTabBar::tab { padding: 9px 16px; color: ${muted}; border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: ${text}; border-bottom: 2px solid ${accent}; }
        QPlainTextEdit, QTextBrowser { background: ${surface}; color: ${text}; border: 1px solid ${border}; border-radius: 6px; padding: 8px; }
        QGroupBox { border: 1px solid ${border}; border-radius: 8px; margin-top: 12px; padding: 14px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: ${muted}; }
        QGroupBox[settingsCard="true"] { padding: 8px; margin-top: 10px; }
        QProgressBar { border: none; border-radius: 3px; background: ${border}; height: 6px; text-align: center; }
        QProgressBar::chunk { background: ${accent}; border-radius: 3px; }
        QSplitter::handle { background: transparent; width: 10px; height: 10px; }
        QLabel[muted="true"] { color: ${muted}; }
        QLabel[warning="true"] { color: ${warning}; }
        QListWidget#reviewFiles { background: ${surface}; border: 1px solid ${border}; border-radius: 6px; }
        QListWidget#reviewFiles::item { padding: 8px 12px; margin: 0; }
        QLabel[heading="true"] { font-size: 19px; font-weight: 600; }
        QStatusBar { color: ${muted}; border-top: 1px solid ${border}; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
        QScrollBar::handle:vertical { background: ${border}; min-height: 24px; border-radius: 4px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
    """).substitute(colors, chevron=chevron))
    app.setProperty("redumpAppearance", appearance)
    return colors
