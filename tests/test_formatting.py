import unittest

from redump_engine.formatting import abbreviate_timezone


class TimestampTests(unittest.TestCase):
    def test_saved_windows_timezone_names(self):
        self.assertEqual(abbreviate_timezone("2026-10-25 12:27 Pacific Summer Time"), "2026-10-25 12:27 PDT")
        self.assertEqual(abbreviate_timezone("2026-12-25 12:27 Pacific Standard Time"), "2026-12-25 12:27 PST")
        self.assertEqual(abbreviate_timezone("2026-10-25 12:27 PDT"), "2026-10-25 12:27 PDT")
