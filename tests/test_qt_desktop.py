"""Qt integration checks; the CLI-only environment can omit the desktop extra."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from redump_engine.library_discovery import LibrarySnapshot
from redump_engine.library_models import LibraryItem
from redump_engine.metadata import Manifest, ManifestDisc, payload_files_from_paths, save_manifest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Install the desktop extra for Qt checks")
class QtDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from redump_engine.qt_app import RedumpWindow
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.window = RedumpWindow(settings_path=self.root / "settings.json", autoload=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_failed_shared_manifest_remains_visible_over_image_success(self):
        metadata = self.root / "manifest.json"
        for kind, own_label in (("CHD", "CHD: Passed"), ("RVZ", "Image integrity: Passed"), ("XISO", "Originals: Passed")):
            with self.subTest(kind=kind):
                item = LibraryItem(self.root / f"Game.{kind.lower()}", kind, 1, metadata)
                self.window.cache.labels.update({item.path: own_label, metadata: "Manifest: Failed"})
                expected = "Original manifest: Failed" if kind == "XISO" else "Manifest: Failed"
                self.assertEqual(expected, self.window.verification(item))
                self.window.cache.labels[item.path] = "Image: Failed"
                self.window.cache.labels[metadata] = "Manifest: Warning"
                self.assertEqual("Image: Failed", self.window.verification(item))
                self.window.cache.labels[item.path] = own_label
                self.window.cache.labels[metadata] = "Manifest: Passed"
                self.assertEqual(own_label, self.window.verification(item))

    def test_group_verification_scope_is_visible_in_text_and_tooltip(self):
        from redump_engine.qt_library import COLUMN_INDEX
        items = [LibraryItem(self.root / f"Game (Disc {number}).rvz", "RVZ", 100, None, system="gamecube") for number in (1, 2)]
        for item in items:
            self.window.cache.labels[item.path] = "Image integrity: Passed"
        self.window.library.set_snapshot(LibrarySnapshot(items, ("gamecube",)), self.root)
        checked = self.window.library.model.item(0, COLUMN_INDEX["checked"])
        self.assertEqual("✓ All integrity checked", checked.text())
        self.assertEqual(checked.text(), checked.toolTip())
        self.window.cache.labels[items[0].path] = "Disc image: Passed"
        self.window.library.refresh_rows()
        checked = self.window.library.model.item(0, COLUMN_INDEX["checked"])
        self.assertEqual("✓ All checks passed (mixed)", checked.text())
        self.assertEqual(checked.text(), checked.toolTip())

    def test_malformed_manifest_cache_job_does_not_prevent_window_close(self):
        from redump_engine.actions import VerificationMode
        metadata = self.root / "manifest.json"
        metadata.write_text(json.dumps({"title": "Game", "set_type": "iso", "discs": [None]}))
        dats = self.root / "dats"
        dats.mkdir()
        self.window.cache.request(metadata, dats, VerificationMode.MANIFEST, "Manifest: Failed")
        self.window.close()
        deadline = time.monotonic() + 3
        while self.window.isVisible() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.cache.saving)

    def test_fit_columns_targets_results_tab(self):
        from PySide6.QtGui import QStandardItem
        results = self.window.results
        results.model.appendRow([QStandardItem("Game " * 30), QStandardItem("Passed"), QStandardItem("Details " * 50)])
        self.window.tabs.setCurrentWidget(results)
        results.table.setColumnWidth(0, 40)
        results.table.setColumnWidth(2, 40)
        library_width = self.window.library.table.columnWidth(0)
        self.window.fit_columns()
        self.assertGreater(results.table.columnWidth(0), 40)
        self.assertGreater(results.table.columnWidth(2), 40)
        self.assertEqual(library_width, self.window.library.table.columnWidth(0))
        self.assertTrue(results.table.horizontalHeader().stretchLastSection())
        self.app.processEvents()
        self.assertGreaterEqual(results.table.horizontalHeader().length(), results.table.viewport().width() - 2)

    def test_conversion_review_options_are_batch_only(self):
        from redump_engine.actions import ACTIONS
        from redump_engine.qt_action_review import ReviewDialog, review_text
        defaults = {"keep_sources": True, "force": False, "use_redump_names": False}
        files = [self.root / "Game.iso"]
        for name in ("convert", "extract", "xiso", "rvz", "rvz-extract"):
            action = ACTIONS[name]
            dialog = ReviewDialog(self.window, action.label, review_text(action, files, defaults), files=files,
                                  action=action, settings=defaults)
            self.assertTrue(dialog.batch_options()["keep_sources"])
            self.assertEqual(action.allows_overwrite, "force" in dialog.option_fields)
            self.assertEqual(name in {"convert", "xiso", "rvz", "rvz-extract"}, "use_redump_names" in dialog.option_fields)
            dialog.option_fields["keep_sources"].setChecked(False)
            self.assertFalse(dialog.batch_options()["keep_sources"])
            self.assertTrue(defaults["keep_sources"])
            dialog.reject()

    def test_pending_stop_can_be_cancelled(self):
        window = self.window
        window.runner.stop_file = self.root / "stop-request"
        window.busy = True
        window.stop()
        self.assertTrue(window.runner.stop_file.exists())
        self.assertEqual("Continue batch", window.stop_button.text())
        window.stop()
        self.assertFalse(window.runner.stop_file.exists())
        self.assertFalse(window.stop_pending)
        self.assertEqual("Stop after current game", window.stop_button.text())
        window.busy = False

    def test_system_column_and_removed_inspector(self):
        from PySide6.QtCore import Qt
        library = self.window.library
        self.assertEqual("System", library.model.headerData(8, Qt.Orientation.Horizontal))
        self.assertEqual(2, library.table.header().visualIndex(8))
        self.assertEqual(2, library.splitter.count())
        library.table.sortByColumn(8, Qt.SortOrder.AscendingOrder)
        saved = library.saved_layout()
        self.assertEqual("system", saved["sort_column"])
        self.assertNotIn("inspector_open", saved)

    def test_settings_pages_and_scan_menu_location(self):
        from PySide6.QtWidgets import QMenu, QTabWidget

        from redump_engine.qt_settings import SettingsDialog
        dialog = SettingsDialog(self.window, self.window.settings, lambda *_: None, {})
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        self.assertEqual(["DATs and cuesheets", "Tools"], [tabs.tabText(i) for i in range(tabs.count())])
        self.assertNotIn("dats", dialog.fields)
        self.assertNotIn("cuesheets", dialog.fields)
        self.assertIn("appearance", dialog.fields)
        self.assertEqual("Scan subfolders", self.window.recursive_action.text())
        file = next(action.menu() for action in self.window.menuBar().actions() if action.text() == "&File")
        assert isinstance(file, QMenu)
        self.assertIn(self.window.recursive_action, file.actions())
        dialog.reject()

    def test_settings_close_keeps_fonts_and_theme_stable(self):
        from PySide6.QtCore import QTimer

        window = self.window
        widgets = [window.folder_combo, window.library.table, window.results.table]
        fonts = [widget.font().toString() for widget in widgets]
        original_theme = window.current_appearance

        def close_settings():
            dialog = window.settings_dialog
            assert dialog is not None
            dialog.reject()

        QTimer.singleShot(0, close_settings)
        window.show_settings()
        self.assertEqual(fonts, [widget.font().toString() for widget in widgets])
        self.assertEqual(original_theme, window.current_appearance)
        window.preview_theme("Dark" if original_theme == "Light" else "Light")
        window.preview_theme(original_theme)
        self.assertEqual(fonts, [widget.font().toString() for widget in widgets])

    def test_log_tab_is_always_available_without_menu_toggle(self):
        self.assertTrue(self.window.results.diagnostics.isTabVisible(1))
        self.assertEqual("Log", self.window.results.diagnostics.tabText(1))
        self.window.persist()
        self.assertNotIn("show_log", json.loads(self.window.settings_path.read_text()))
        from PySide6.QtGui import QAction
        self.assertFalse(any(action.text() == "Show log" for action in self.window.findChildren(QAction)))

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_group_sorting_and_select_all_include_collapsed_discs(self):
        from PySide6.QtCore import Qt
        cases = [("A (Disc 1).chd", 100, 1000), ("A (Disc 2).chd", 900, 1000), ("B.chd", 600, 1000),
                 ("C (Disc 1).chd", 450, 1000), ("C (Disc 2).chd", 50, 1000), ("D.chd", 700, None)]
        items = [LibraryItem(self.root / name, "CHD", size, None, original_size=original, system="ps2") for name, size, original in cases]
        library = self.window.library
        library.set_snapshot(LibrarySnapshot(items, ("ps2",)), self.root)
        def names():
            return [library.proxy.index(row, 0).data() for row in range(library.proxy.rowCount())]
        library.table.sortByColumn(3, Qt.SortOrder.AscendingOrder)
        self.assertEqual(names(), ["C", "B", "D", "A"])
        library.table.sortByColumn(5, Qt.SortOrder.DescendingOrder)
        self.assertEqual(names(), ["C", "A", "B", "D"])
        parent = library.proxy.index(0, 0)
        self.assertEqual([library.proxy.index(row, 0, parent).data() for row in range(2)], ["Disc 1", "Disc 2"])
        library.expand_sets(False)
        library.select_all()
        self.assertEqual({item.path for item in library.selected()}, {item.path for item in items})
        library.query = "B.chd"
        library.refresh_rows()
        library.select_all()
        self.assertEqual([item.path.name for item in library.selected()], ["B.chd"])

    def test_settings_cancel_restores_preview_and_ok_commits(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QComboBox
        window = self.window
        def edit_then_cancel():
            dialog = window.settings_dialog
            assert dialog is not None
            appearance = dialog.fields["appearance"]
            assert isinstance(appearance, QComboBox)
            self.assertNotIn("keep_sources", dialog.fields)
            appearance.setCurrentText("Dark")
            dialog.reject()
        QTimer.singleShot(0, edit_then_cancel)
        window.show_settings()
        self.assertEqual(window.settings["appearance"], "Light")
        self.assertTrue(window.settings["keep_sources"])
        def edit_then_accept():
            dialog = window.settings_dialog
            assert dialog is not None
            appearance = dialog.fields["appearance"]
            assert isinstance(appearance, QComboBox)
            appearance.setCurrentText("Dark")
            dialog.accept()
        QTimer.singleShot(0, edit_then_accept)
        window.show_settings()
        saved = json.loads(window.settings_path.read_text())
        self.assertEqual(saved["appearance"], "Dark")
        self.assertNotIn("keep_sources", saved)

    def test_details_reuses_window_and_updates_text(self):
        self.window.show_text("Disc details", "First selection")
        first = self.window.dialogs["Disc details"]
        self.window.show_text("Disc details", "New selection")
        self.assertIs(first, self.window.dialogs["Disc details"])
        self.assertEqual(first.viewer.toPlainText(), "New selection")

    def test_real_verify_batch_and_saved_status(self):
        from PySide6.QtCore import QTimer
        library = self.root / "library"
        dats = self.root / "dats"
        dats.mkdir()
        games = []
        for number in range(12):
            title = f"Game {number:02d} (USA)"
            folder = library / "Sony - PlayStation 2" / title
            folder.mkdir(parents=True)
            image = folder / f"{title}.iso"
            image.write_bytes(f"disc {number}".encode())
            payload = payload_files_from_paths([image], "iso")
            save_manifest(folder / "manifest.json", Manifest(1, "iso", title, [ManifestDisc(1, payload, [image.name])], "ps2"))
            games.append(f'<game name="{title}"><rom name="{image.name}" size="{payload[0]["size"]}" sha1="{payload[0]["sha1"]}" crc="{payload[0]["crc32"]}"/></game>')
        (dats / "ps2.dat").write_text("<datafile>" + "".join(games) + "</datafile>")
        window = self.window
        window.settings["dats"] = str(dats)
        window.choose_path(str(library))
        self.wait(lambda: not window.scanning and len(window.library.items) == 24)
        window.library.select_all()
        self.assertEqual(len(window.action_paths("verify")), 12)
        ticks = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
        heartbeat.start(10)
        window.start_action("verify")
        self.wait(lambda: not window.busy and len(window.results.records) == 12)
        self.wait(lambda: not window.cache.saving)
        heartbeat.stop()
        self.assertTrue(all(record.status == "Passed" for record in window.results.records), window.results.records)
        self.assertGreater(len(ticks), 2)
        window.persist()
        window.persist_cache()
        from redump_engine.ui_settings import load_preferences
        self.assertNotIn("verification_cache", json.loads(window.settings_path.read_text()))
        from redump_engine.verification_storage import load_verification_records
        self.assertNotIn("verification_cache", load_preferences(window.settings_path))
        self.assertEqual(len(load_verification_records(window.cache_path)), 12)
        from unittest.mock import patch

        from redump_engine.qt_app import RedumpWindow
        with patch("redump_engine.qt_app.DAT_ROOT", dats):
            reopened = RedumpWindow(settings_path=window.settings_path, autoload=False)
        try:
            reopened.choose_path(str(library))
            self.wait(lambda: not reopened.scanning and len(reopened.cache.labels) == 12)
            self.assertTrue(all(reopened.verification(item) == "Manifest: Passed" for item in reopened.library.items))
        finally:
            reopened.close()

    def test_tabbed_diagnostics_copy_and_empty_state(self):
        from PySide6.QtWidgets import QApplication
        panel = self.window.results
        self.assertTrue(panel.diagnostics.isTabVisible(1))
        panel.diagnostics.setCurrentIndex(1)
        self.assertTrue(panel.diagnostics.isTabVisible(1))
        self.assertEqual("Log", panel.diagnostics.tabText(1))
        self.assertFalse(panel.copy.isEnabled())
        panel.append_log("First line")
        panel.append_log("Second line")
        self.assertTrue(panel.copy.isEnabled())
        panel.copy.click()
        self.assertEqual("First line\nSecond line", QApplication.clipboard().text())
        self.assertEqual("Copied", panel.copy.text())
        panel.diagnostics.setCurrentIndex(0)
        self.assertEqual("Copy", panel.copy.text())
        self.assertFalse(panel.copy.isEnabled())
        self.assertTrue(panel.diagnostics.isTabVisible(1))

    def test_conversion_rechecks_manifest_after_source_deleted(self):
        from redump_engine.events import ResultRecord
        from redump_engine.library import item_for
        library = self.root / "library"
        folder = library / "Sony - PlayStation 2" / "Game (USA)"
        folder.mkdir(parents=True)
        iso = folder / "Game (USA).iso"
        iso.write_bytes(b"original image")
        payload = payload_files_from_paths([iso], "iso")
        metadata = folder / "manifest.json"
        save_manifest(metadata, Manifest(1, "iso", "Game (USA)", [ManifestDisc(1, payload, [iso.name])], "ps2"))
        dats = self.root / "dats"
        dats.mkdir()
        rom = payload[0]
        (dats / "ps2.dat").write_text(f'<datafile><game name="Game (USA)"><rom name="{iso.name}" size="{rom["size"]}" sha1="{rom["sha1"]}" crc="{rom["crc32"]}"/></game></datafile>')
        chd = iso.with_suffix(".chd")
        chd.write_bytes(b"compressed image")
        iso.unlink()
        window = self.window
        window.settings["dats"] = str(dats)
        window.choose_path(str(library))
        self.wait(lambda: not window.scanning)
        window.results.records = [ResultRecord(iso.name, "Completed", "Converted", folder, reference=metadata, source=chd)]
        window.runner.last_command = window.command("convert")
        window.finish(0)
        self.wait(lambda: window.cache.labels.get(metadata) == "Manifest: Passed")
        self.assertEqual("Manifest: Passed", window.verification(item_for(chd)))
        self.assertEqual("manifest", window.cache.records[str(metadata)]["mode"])
        self.assertFalse(iso.exists())

    def test_details_copy_empty_state_and_feedback_reset(self):
        from PySide6.QtWidgets import QApplication, QPushButton

        from redump_engine.qt_details import TextDialog
        dialog = TextDialog(self.window, "Disc details")
        copy = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Copy")
        self.assertFalse(copy.isEnabled())
        dialog.set_text("First disc")
        self.assertTrue(copy.isEnabled())
        copy.click()
        self.assertEqual("Copied", copy.text())
        self.assertEqual("First disc", QApplication.clipboard().text())
        dialog.set_text("Second disc")
        self.assertEqual("Copy", copy.text())
        dialog.set_text("")
        self.assertFalse(copy.isEnabled())
        dialog.close()

    def test_empty_conversion_review_cannot_start(self):
        from PySide6.QtWidgets import QDialogButtonBox

        from redump_engine.actions import ACTIONS
        from redump_engine.qt_action_review import ReviewDialog
        dialog = ReviewDialog(self.window, "Convert ISO to RVZ", "Selected: 0", files=[], action=ACTIONS["rvz"])
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        start = next(button for button in buttons.buttons() if button.text() == "Start")
        self.assertFalse(start.isEnabled())
        dialog.close()

    def test_shared_dialog_spacing_and_safe_confirmation_defaults(self):
        from PySide6.QtWidgets import QDialogButtonBox

        from redump_engine.qt_action_review import ReviewDialog
        from redump_engine.qt_details import TextDialog
        from redump_engine.qt_settings import SettingsDialog
        dialogs = [TextDialog(self.window, "Details"),
                   ReviewDialog(self.window, "Review", "Changes", accept="Apply changes"),
                   SettingsDialog(self.window, self.window.settings, lambda *_: None, {})]
        try:
            layouts = []
            for dialog in dialogs:
                layout = dialog.layout()
                assert layout is not None
                layouts.append(layout)
            margins = [layout.contentsMargins() for layout in layouts]
            self.assertTrue(all(margin == margins[0] for margin in margins))
            self.assertEqual([12, 12, 12], [layout.spacing() for layout in layouts])
            buttons = dialogs[1].findChild(QDialogButtonBox)
            assert buttons is not None
            self.assertTrue(buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault())
            self.assertFalse(next(button for button in buttons.buttons() if button.text() == "Apply changes").autoDefault())
        finally:
            for dialog in dialogs:
                dialog.close()

    def test_search_rows_and_diagnostic_tabs_share_spacing(self):
        from redump_engine.qt_layout import CONTROL_GAP, TAB_MARGINS
        for panel in (self.window.library, self.window.results):
            layout = panel.layout()
            assert layout is not None
            margins = layout.contentsMargins()
            self.assertEqual(TAB_MARGINS, (margins.left(), margins.top(), margins.right(), margins.bottom()))
            self.assertEqual(CONTROL_GAP, layout.spacing())
            item = layout.itemAt(0)
            assert item is not None
            row = item.layout()
            assert row is not None
            self.assertEqual(CONTROL_GAP, row.spacing())
            self.assertEqual(0, row.contentsMargins().left())
        for index in range(self.window.results.diagnostics.count()):
            page = self.window.results.diagnostics.widget(index)
            assert page is not None
            layout = page.layout()
            assert layout is not None
            self.assertEqual(CONTROL_GAP, layout.contentsMargins().top())

    def test_results_and_library_share_header_and_row_style(self):
        from PySide6.QtCore import Qt
        library = self.window.library.table
        results = self.window.results.table
        self.assertEqual(library.header().defaultAlignment(), results.horizontalHeader().defaultAlignment())
        self.assertTrue(results.horizontalHeader().defaultAlignment() & Qt.AlignmentFlag.AlignLeft)
        self.assertFalse(results.showGrid())
        self.assertEqual(library.selectionBehavior(), results.selectionBehavior())
        self.assertEqual(library.editTriggers(), results.editTriggers())

    def test_game_and_filename_are_separate_and_filename_visibility_persists(self):
        from redump_engine.library import item_for
        from redump_engine.qt_library import COLUMN_INDEX, LibraryPanel
        folder = self.root / "Sony - PlayStation 2" / "Official Game (USA)"
        folder.mkdir(parents=True)
        image = folder / "old-file-name.iso"
        image.write_bytes(b"disc")
        payload = payload_files_from_paths([image], "iso")
        save_manifest(folder / "manifest.json", Manifest(1, "iso", "Official Game (USA)", [ManifestDisc(1, payload, [image.name])], "ps2"))
        item = item_for(image)
        self.assertEqual("Official Game (USA)", item.game_title)
        panel = self.window.library
        panel.root = self.root
        panel.items = [item]
        panel.refresh_rows()
        self.assertEqual("Game", panel.model.horizontalHeaderItem(0).text())
        self.assertEqual("Official Game (USA)", panel.model.item(0, 0).text())
        filename = COLUMN_INDEX["filename"]
        self.assertEqual(image.name, panel.model.item(0, filename).text())
        self.assertTrue(panel.table.isColumnHidden(filename))
        panel.set_column_visible(filename, True)
        panel.table.setColumnWidth(filename, 360)
        state = panel.saved_layout()
        state["sort_column"] = "filename"
        reopened = LibraryPanel(state, self.window.verification, self.window.colors)
        try:
            self.assertFalse(reopened.table.isColumnHidden(filename))
            self.assertEqual(360, reopened.table.columnWidth(filename))
            self.assertEqual(filename, reopened.table.header().sortIndicatorSection())
        finally:
            reopened.close()
        panel.query = "Official Game"
        panel.refresh_rows()
        self.assertEqual(1, panel.model.rowCount())

    def test_automatic_recheck_continues_after_bad_manifest_and_reports_error(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from redump_engine.dat_models import DatIndex
        paths = []
        for name in ("Bad", "Good"):
            folder = self.root / name
            folder.mkdir()
            path = folder / "manifest.json"
            path.write_text("{}")
            paths.append(path)
        bad, good = paths
        self.window.cache.records[str(bad)] = {"old": "record"}
        self.window.cache.labels[bad] = "Manifest: Passed"
        def check(folder, path):
            if path == bad:
                raise ValueError("bad manifest")
            return SimpleNamespace(status="passed")
        with (patch("redump_engine.dat_loader.load_dat_index", return_value=DatIndex([], {})),
              patch("redump_engine.workflow_verify.VerifyWorkflow.process_game_dir", side_effect=check),
              patch("redump_engine.metadata.load_manifest", return_value=None)):
            self.window.recheck_manifests(set(paths))
            self.wait(lambda: self.window.cache.labels.get(good) == "Manifest: Passed" and bool(self.window.results.records))
        self.assertNotIn(bad, self.window.cache.labels)
        self.assertNotIn(str(bad), self.window.cache.records)
        self.assertIn("bad manifest", self.window.results.log.toPlainText())
        self.assertEqual("Warning", self.window.results.records[-1].status)

    def test_recheck_rejects_manifest_changed_while_result_waits_in_queue(self):
        from redump_engine.metadata import load_dat_index, load_manifest
        from redump_engine.workflow_verify import VerifyWorkflow
        window = self.window
        window.timer.stop()
        folder = self.root / "Sony - PlayStation 2" / "Game"
        folder.mkdir(parents=True)
        image = folder / "Game.iso"
        image.write_bytes(b"original image")
        payloads = payload_files_from_paths([image], "iso")
        metadata = folder / "manifest.json"
        save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, payloads, [image.name])], "ps2"))
        dats = self.root / "dats"
        dats.mkdir()
        rom = payloads[0]
        (dats / "game.dat").write_text(f'<datafile><header><name>Sony - PlayStation 2</name></header><game name="Game"><rom name="Game.iso" size="{rom["size"]}" sha1="{rom["sha1"]}" crc="{rom["crc32"]}"/></game></datafile>')
        window.settings["dats"] = str(dats)
        window.recheck_manifests({metadata})
        self.wait(lambda: not window.jobs.empty())
        manifest = load_manifest(metadata)
        assert manifest is not None
        manifest.disc_entries[0].payload_files[0]["sha1"] = "f" * 40
        save_manifest(metadata, manifest)
        window.poll()
        self.wait(lambda: (window.poll(), not window.cache.saving)[1])
        self.assertNotIn(metadata, window.cache.labels)
        self.assertIsNone(window.cache.lookup(metadata, dats))
        self.assertEqual("failed", VerifyWorkflow(load_dat_index(dats)).process_game_dir(folder).status)

    def test_results_show_system_and_operation_columns(self):
        from redump_engine.events import ResultRecord
        panel = self.window.results
        panel.records = [ResultRecord("Game", "Passed", "Matched", system="xbox", operation="verify"),
                         ResultRecord("Other", "Passed", "Checked", system="dc", operation="chd-integrity")]
        panel.refresh_rows()
        self.assertEqual(["Game / item", "System", "Operation", "Status", "Details"],
                         [panel.model.horizontalHeaderItem(index).text() for index in range(panel.model.columnCount())])
        self.assertEqual("Xbox", panel.model.item(0, 1).text())
        self.assertEqual("Verify manifest against Redump DAT", panel.model.item(0, 2).text())
        panel.search.setText("Dreamcast")
        self.assertEqual(1, panel.model.rowCount())
        self.assertEqual("Other", panel.model.item(0, 0).text())
        panel.search.setText("integrity")
        self.assertEqual(1, panel.model.rowCount())

    def test_actual_tool_progress_displays_percentage_once(self):
        import contextlib
        import io

        from redump_engine.events import EventReporter, ToolProgress
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            EventReporter(True).tool_progress(ToolProgress("createcd", self.root / "Game.cue", 21.5,
                                                         "Compressing, 21.5% complete... (ratio=0.6%)"))
        self.window.events.put(("log", output.getvalue()))
        self.window.poll()
        self.assertEqual(1, self.window.task_title.text().count("21.5%"))
        self.assertIn("Game.cue", self.window.task_title.text())
