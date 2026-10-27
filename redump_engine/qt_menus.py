"""WindowMenus: focused orchestration for the desktop window."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMenu,
)

from .actions import ACTIONS, GROUPS
from .ui_settings import load_recent_folders

if TYPE_CHECKING:
    from .qt_app import RedumpWindow


class WindowMenus:
    def __init__(self, window: RedumpWindow):
        self.window = window

    def add_action(self, menu: QMenu, text: str, callback, shortcut: str | None = None) -> QAction:
        window = self.window
        action = menu.addAction(text)
        action.triggered.connect(lambda checked=False: callback())
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            window.addAction(action)
        return action


    def build_menus(self) -> None:
        window = self.window
        file = window.menuBar().addMenu("&File")
        window.add_action(file, "Change library folder…", window.choose_folder, "Ctrl+O")
        window.recent_menu = file.addMenu("Recent folders")
        file.aboutToShow.connect(window.update_recent)
        file.addSeparator()
        window.recursive_action = file.addAction("Scan subfolders")
        window.recursive_action.setCheckable(True)
        window.recursive_action.setChecked(window.library.recursive)
        window.recursive_action.setToolTip("Scan the library folder and all folders beneath it")
        window.recursive_action.toggled.connect(window.set_recursive)
        file.addSeparator()
        window.add_action(file, "Open folder in Explorer", lambda: window.open_path(window.root_folder))
        file.addSeparator()
        window.add_action(file, "Exit", window.close)
        window.action_menu = window.menuBar().addMenu("&Actions")
        for group, title in GROUPS:
            menu = window.action_menu.addMenu(title)
            for spec in ACTIONS.values():
                if spec.group == group:
                    window.operation_actions[spec.id] = window.add_action(menu, spec.label, lambda name=spec.id: window.start_action(name))
        window.action_menu.addSeparator()
        window.operation_actions["rehash"] = window.add_action(window.action_menu, "Verify original files against Redump DAT", lambda: window.start_action("rehash"))
        window.action_menu.addSeparator()
        window.operation_actions["organize"] = window.add_action(window.action_menu, "Fix names and folders…", lambda: window.start_action("organize"))
        window.action_menu.addSeparator()
        window.disc_details_action = window.add_action(window.action_menu, "View disc details…", window.show_disc_details)
        window.open_manifest_action = window.add_action(window.action_menu, "Open manifest", window.open_manifest)
        window.open_folder_action = window.add_action(window.action_menu, "Open game folder", window.open_game_folder)
        window.action_menu.addSeparator()
        window.select_all_action = window.add_action(window.action_menu, "Select all discs", window.library.select_all)
        window.action_menu.addSeparator()
        window.operation_actions["delete"] = window.add_action(window.action_menu, "Delete selected…", lambda: window.start_action("delete"))
        window.action_menu.aboutToShow.connect(window.update_action_states)
        view = window.menuBar().addMenu("&View")
        window.group_action = view.addAction("Group multi-disc games")
        window.group_action.setCheckable(True)
        window.group_action.setChecked(window.library.grouped)
        window.group_action.toggled.connect(window.set_grouped)
        view.addSeparator()
        fit = window.add_action(view, "Fit columns to contents", window.fit_columns, "Ctrl++")
        fit.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        window.add_action(view, "Refresh library", window.refresh_library_tab, "F5")
        window.add_action(view, "Expand all sets", lambda: window.library.expand_sets(True))
        window.add_action(view, "Collapse all sets", lambda: window.library.expand_sets(False))
        columns = view.addMenu("Columns")
        for column in sorted(range(1, window.library.model.columnCount()),
                             key=lambda index: str(window.library.model.headerData(index, Qt.Orientation.Horizontal)).casefold()):
            action = columns.addAction(str(window.library.model.headerData(column, Qt.Orientation.Horizontal)))
            action.setCheckable(True)
            action.setChecked(not window.library.table.isColumnHidden(column))
            action.toggled.connect(lambda visible, column=column: window.library.set_column_visible(column, visible))
        tools = window.menuBar().addMenu("&Tools")
        window.update_action = window.add_action(tools, "Refresh DATs and cuesheets…", lambda: window.start_update(window.settings))
        tools.addSeparator()
        window.add_action(tools, "Settings…", window.show_settings)
        help_menu = window.menuBar().addMenu("&Help")
        window.add_action(help_menu, "Quick guide", lambda: window.show_text("Quick guide", "Open a collection folder, select discs, then choose Actions or right-click.\n\n"
            "Verify compares manifest records with Redump DATs; CHD integrity checks the compressed image.\n"
            "Fix names and folders reviews Redump naming and game-folder changes before applying. Create manifests records original checksums.\n\n"
            "Settings saves only with OK. Cancel and Escape discard edits.\n"
            "Reference refresh updates DATs and cuesheets together; downloads are not undone by Cancel."))
        window.add_action(help_menu, "Keyboard shortcuts", lambda: window.show_text("Keyboard shortcuts", "Ctrl+O   Change library folder\nF5   Refresh library\n"
            "Ctrl+A   Select all discs, including collapsed sets\nCtrl++   Fit columns to contents\nDouble-click a column divider   Fit that column\nShift+F10   Selection actions\nEsc   Cancel a dialog"))
        window.add_action(help_menu, "About", lambda: window.show_text("About Redump Tools", "Redump Tools\n\nA disc-image library manager built with Qt for Python.\n"
            "CHD, RVZ, Xbox XISO, and original ISO / BIN+CUE workflows.\n\nConversion and verification run through the existing command-line engine."))


    def update_recent(self) -> None:
        window = self.window
        window.recent_menu.clear()
        for folder in load_recent_folders(window.settings_path):
            action = window.add_action(window.recent_menu, folder, lambda folder=folder: window.choose_path(folder))
            action.setEnabled(not window.busy and not window.reviewing)
