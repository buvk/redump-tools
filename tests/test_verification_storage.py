import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.ui_settings import load_preferences, save_preferences
from redump_engine.verification_storage import (
    load_verification_records,
    pack_records,
    save_verification_records,
    unpack_records,
    verification_cache_path,
)


class VerificationStorageTests(unittest.TestCase):
    def records(self):
        dats = ["D:/DATs", [["psx.zip", 123, 456, 789]]]
        return {f"Game {i}.chd": {"mode": "manifest", "label": "Manifest: Passed", "inputs": {"file": [i, 2, 3], "dats": dats}}
                for i in range(3)}

    def test_shared_dats_are_stored_once_and_round_trip(self):
        records = self.records()
        packed = pack_records(records)
        self.assertEqual(1, len(packed["dat_fingerprints"]))
        self.assertEqual(records, unpack_records(json.loads(json.dumps(packed))))
        self.assertIn("dats", records["Game 0.chd"]["inputs"])
        self.assertNotIn("dats_ref", records["Game 0.chd"]["inputs"])

    def test_missing_shared_fingerprint_drops_only_affected_record(self):
        packed = pack_records(self.records())
        packed["records"]["Game 0.chd"]["inputs"]["dats_ref"] = "missing"
        self.assertEqual(2, len(unpack_records(packed) or {}))

    def test_missing_dat_reference_keeps_independent_integrity_check(self):
        integrity = {"mode": "chd", "label": "CHD: Passed", "inputs": {"file": [1, 2, 3]}}
        dat_check = self.records()["Game 0.chd"]
        record = {**dat_check, "checks": {"manifest": dat_check, "chd": integrity}}
        packed = pack_records({"Game.chd": record})
        packed["dat_fingerprints"].clear()
        restored = unpack_records(packed)
        self.assertEqual({"Game.chd": {**integrity, "checks": {"chd": integrity}}}, restored)

    def test_cache_survives_settings_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            cache = verification_cache_path(settings)
            records = self.records()
            save_verification_records(records, cache)
            save_preferences({"appearance": "Dark"}, settings)
            self.assertNotIn("verification_cache", load_preferences(settings))
            settings.unlink()
            self.assertEqual(records, load_verification_records(cache))

    def test_failed_cache_write_keeps_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "verification.json"
            save_verification_records(self.records(), cache)
            original = cache.read_bytes()
            with patch("redump_engine.json_storage.json.dump", side_effect=OSError("full disk")):
                with self.assertRaises(OSError):
                    save_verification_records({}, cache)
            self.assertEqual(original, cache.read_bytes())
            self.assertEqual([cache], list(cache.parent.iterdir()))

    def test_settings_do_not_access_cache_and_cache_does_not_touch_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            cache = verification_cache_path(settings)
            save_preferences({"refresh_system": "ps2"}, settings)
            before = settings.read_bytes()
            save_verification_records(self.records(), cache)
            self.assertEqual(before, settings.read_bytes())
            with patch("redump_engine.verification_storage.read_object", side_effect=AssertionError("cache accessed")):
                save_preferences({"appearance": "Dark"}, settings)
                self.assertEqual("ps2", load_preferences(settings)["refresh_system"])

    def test_missing_or_invalid_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "verification.json"
            self.assertEqual({}, load_verification_records(cache))
            cache.write_text("invalid JSON")
            self.assertEqual({}, load_verification_records(cache))
