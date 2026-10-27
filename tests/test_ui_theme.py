from __future__ import annotations

import importlib.util
import unittest

from redump_engine.ui_badges import result_badge, verification_badge


def luminance(color: str) -> float:
    values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
    return sum(value * factor for value, factor in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def contrast(first: str, second: str) -> float:
    bright, dim = sorted((luminance(first), luminance(second)), reverse=True)
    return (bright + 0.05) / (dim + 0.05)


class ModernPresentationTests(unittest.TestCase):
    def test_verification_badges_preserve_what_was_checked(self) -> None:
        self.assertEqual(("✓ CHD checked", "Success"), verification_badge("CHD: Passed"))
        self.assertEqual(("✓ DAT match", "Success"), verification_badge("Manifest: Passed"))
        self.assertEqual(("✓ Original DAT match", "Success"), verification_badge("Original manifest: Passed"))
        self.assertEqual(("✓ Original DAT match", "Success"), verification_badge("Original files: Passed"))
        self.assertEqual(("! Names/folders need attention", "Warning"), verification_badge("Manifest: Warning"))
        self.assertEqual(("! Failed", "Error"), verification_badge("CHD: Failed"))
        self.assertEqual(("○ Not checked", "Neutral"), verification_badge("Not checked"))

    def test_result_badges_are_visual_labels_without_changing_records(self) -> None:
        self.assertEqual("! Needs attention", result_badge("Warning"))
        self.assertEqual("✓ Verified", result_badge("Passed"))
        self.assertEqual("○ Preview", result_badge("Planned"))

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt desktop extra required")
    def test_both_palettes_keep_readable_text_and_selection_contrast(self) -> None:
        from redump_engine.qt_theme import theme_colors
        for palette in (theme_colors("Light"), theme_colors("Dark")):
            with self.subTest(palette=palette):
                self.assertGreaterEqual(contrast(palette["text"], palette["surface"]), 4.5)
                self.assertGreaterEqual(contrast(palette["muted"], palette["background"]), 4.5)
                self.assertGreaterEqual(contrast(palette["text"], palette["selection"]), 4.5)
                self.assertGreaterEqual(contrast("#ffffff", palette["accent"]), 4.5)
                for color in (palette["success"], palette["warning"], palette["error"]):
                    self.assertGreaterEqual(contrast(color, palette["alternate"]), 4.5)
