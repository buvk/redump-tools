"""Invalid manifest structures must fail predictably before reaching workers."""
import json
import unittest

from redump_engine.metadata import manifest_from_dict


class ManifestValidationTests(unittest.TestCase):
    def test_malformed_structures_raise_value_error(self):
        base = {"title": "Game", "set_type": "iso", "discs": []}
        invalid = [None, [], {**base, "title": []}, {**base, "set_type": None}, {**base, "schema_version": False},
                   {**base, "discs": None}, {**base, "discs": {}}, {**base, "discs": [None]}]
        invalid.extend({**base, "discs": [disc]} for disc in (
            {}, {"disc": None}, {"disc": False}, {"disc": 0}, {"disc": -1}, {"disc": 1.5}, {"disc": "bad"},
            {"disc": 1, "payload_files": None}, {"disc": 1, "payload_files": {}}, {"disc": 1, "payload_files": [None]},
            {"disc": 1, "source_files": None}, {"disc": 1, "source_files": {}}, {"disc": 1, "source_files": [1]}))
        invalid.append({**base, "discs": [{"disc": 1}, {"disc": "1"}]})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                manifest_from_dict(json.loads(json.dumps(value)))

    def test_incomplete_but_well_formed_records_remain_readable(self):
        manifest = manifest_from_dict({"title": "Game", "set_type": "mixed", "discs": [
            {"disc": "2", "payload_files": [], "source_files": ["backup.chd"]}]})
        self.assertEqual(2, manifest.disc_entries[0].disc)
        self.assertEqual([], manifest.disc_entries[0].payload_files)
        self.assertEqual(["backup.chd"], manifest.disc_entries[0].source_files)
