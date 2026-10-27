import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from redump_engine.actions import VerificationMode
from redump_engine.cuesheets import CueRepository
from redump_engine.events import ResultRecord
from redump_engine.verification_cache import VerificationCache, VerificationRecords


class RememberedVerificationTests(unittest.TestCase):
    def test_malformed_manifest_does_not_stop_worker_or_leave_saving_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dats = root / "dats"
            dats.mkdir()
            metadata = root / "manifest.json"
            metadata.write_text(json.dumps({"title": "Game", "set_type": "iso", "discs": [None]}))
            image = root / "Game.chd"
            image.write_bytes(b"image")
            cache = VerificationCache()
            self.addCleanup(cache.close)
            cache.request(metadata, dats, VerificationMode.MANIFEST, "Manifest: Failed")
            cache.request(image, dats, VerificationMode.CHD, "CHD: Passed")
            deadline = time.monotonic() + 3
            while cache.saving and time.monotonic() < deadline:
                cache.drain()
                time.sleep(0.005)
            self.assertFalse(cache.saving)
            self.assertEqual("CHD: Passed", cache.labels.get(image))
            assert cache._worker is not None
            self.assertTrue(cache._worker.is_alive())

    def test_unexpected_job_failure_invalidates_proof_and_worker_continues(self):
        from redump_engine.verification_cache import signature
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad, good = root / "bad.chd", root / "good.chd"
            bad.write_bytes(b"bad")
            good.write_bytes(b"good")
            cache = VerificationCache()
            self.addCleanup(cache.close)
            def fail_one(path, *args):
                if path == bad:
                    raise RuntimeError("unexpected metadata failure")
                return signature(path, *args)
            with patch("redump_engine.verification_cache.signature", side_effect=fail_one):
                cache.request(bad, root, VerificationMode.CHD, "CHD: Passed")
                cache.request(good, root, VerificationMode.CHD, "CHD: Passed")
                deadline = time.monotonic() + 3
                while cache.saving and time.monotonic() < deadline:
                    cache.drain()
                    time.sleep(0.005)
            self.assertFalse(cache.saving)
            self.assertNotIn(bad, cache.labels)
            self.assertNotIn(str(bad), cache.records)
            self.assertEqual("CHD: Passed", cache.labels.get(good))
            assert cache._worker is not None
            self.assertTrue(cache._worker.is_alive())

    def test_manifest_result_updates_image_and_shared_manifest(self):
        cache = VerificationCache()
        self.addCleanup(cache.close)
        image, metadata, dats = Path("Game.iso"), Path("manifest.json"), Path("dats")
        result = ResultRecord("Game", "Warning", "Naming issue", source=image, reference=metadata,
                              verified_inputs={str(path.resolve()): {"file": [1, 2, 3]} for path in (image, metadata)})
        with patch.object(cache, "request") as request:
            cache.remember_result(result, VerificationMode.MANIFEST, dats)
        self.assertEqual({image, metadata}, {call.args[0] for call in request.call_args_list})
        self.assertTrue(all(call.args[3] == "Manifest: Warning" for call in request.call_args_list))

    def test_image_result_keeps_its_mode_and_does_not_mark_manifest_checked(self):
        cache = VerificationCache()
        self.addCleanup(cache.close)
        image, dats = Path("Game.chd"), Path("dats")
        result = ResultRecord("Game", "Passed", "Integrity checked", source=image, reference=Path("manifest.json"),
                              check_mode=VerificationMode.IMAGE_INTEGRITY, verified_inputs={str(image.resolve()): {"file": [1, 2, 3]}})
        with patch.object(cache, "request") as request:
            cache.remember_result(result, VerificationMode.CHD, dats)
        request.assert_called_once_with(image, dats, VerificationMode.CHD, "Image integrity: Passed", checked_inputs={"file": [1, 2, 3]})

    def test_slow_filesystem_checks_run_off_caller_thread(self):
        cache = VerificationCache()
        self.addCleanup(cache.close)
        started, release = threading.Event(), threading.Event()
        caller = threading.get_ident()

        def slow_signature(*args):
            self.assertNotEqual(threading.get_ident(), caller)
            started.set()
            self.assertTrue(release.wait(3))
            return {"file": [1, 2, 3]}

        path = Path("C:/example/game.chd")
        with patch("redump_engine.verification_cache.signature", side_effect=slow_signature):
            cache.request(path, Path("C:/dats"), VerificationMode.CHD, "CHD: Passed")
            self.assertTrue(started.wait(3))
            # Rendering/draining must not wait for a blocked filesystem.
            self.assertEqual(cache.drain(), (False, False))
            self.assertIsNone(cache.labels.get(path))
            cache.request(path, Path("C:/dats"))  # A scan must not supersede the pending result.
            release.set()
            deadline = time.monotonic() + 3
            while path not in cache.labels and time.monotonic() < deadline:
                cache.drain()
                time.sleep(.01)
            self.assertEqual(cache.labels.get(path), "CHD: Passed")
            self.assertIn(str(path), cache.records)

    def test_pending_jobs_coalesce_and_worker_stops(self):
        cache = VerificationCache()
        self.addCleanup(cache.close)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        path, dats = Path("C:/example/game.chd"), Path("C:/dats")
        calls = []

        def slow_signature(*args):
            calls.append(args)
            entered.set()
            self.assertTrue(release.wait(3))
            return {"file": [1, 2, 3]}

        with patch("redump_engine.verification_cache.signature", side_effect=slow_signature):
            cache.request(path, dats, VerificationMode.CHD, "Old")
            self.assertTrue(entered.wait(3))
            for number in range(100):
                cache.request(path, dats, VerificationMode.CHD, f"Newest {number}")
            self.assertTrue(cache.saving)
            release.set()
            deadline = time.monotonic() + 3
            while cache.saving and time.monotonic() < deadline:
                cache.drain()
                time.sleep(.01)
            self.assertEqual(cache.labels.get(path), "Newest 99")
            self.assertEqual(len(calls), 2)
            self.assertFalse(cache.saving)
        cache.close()
        assert cache._worker is not None
        cache._worker.join(timeout=3)
        self.assertFalse(cache._worker.is_alive())
        cache.request(path, dats, VerificationMode.CHD, "Ignored after close")
        self.assertFalse(cache.saving)

    def test_reopen_and_invalidate_changed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            dats.mkdir()
            dat = dats / "dc.dat"
            dat.write_text("first")
            game = root / "game"
            game.mkdir()
            manifest = game / "manifest.json"
            manifest.write_text("metadata")
            original = game / "track.bin"
            original.write_bytes(b"original")
            cache = VerificationRecords()
            cache.remember(manifest, VerificationMode.ORIGINALS, "Original files: Passed", dats)
            restored = VerificationRecords(json.loads(json.dumps(cache.records)))
            self.assertEqual(restored.lookup(manifest, dats), "Original files: Passed")
            original.write_bytes(b"changed original")
            self.assertIsNone(restored.lookup(manifest, dats))
            cache.remember(manifest, VerificationMode.MANIFEST, "Manifest: Passed", dats)
            dat.write_text("updated DAT")
            self.assertIsNone(cache.lookup(manifest, dats))

    def test_chd_integrity_does_not_depend_on_dat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "game.chd"
            image.write_bytes(b"image")
            cache = VerificationRecords()
            cache.remember(image, VerificationMode.CHD, "CHD: Passed", root)
            (root / "new.dat").write_text("new")
            self.assertEqual(cache.lookup(image, root), "CHD: Passed")
            image.write_bytes(b"changed image")
            self.assertIsNone(cache.lookup(image, root))

    def test_duplicate_titles_resolve_by_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for system in ("psx", "dc"):
                with ZipFile(root / f"redump_{system}_cuesheets.zip", "w") as archive:
                    archive.writestr("Game (USA).cue", system)
            repo = CueRepository(root)
            self.assertIsNone(repo.find("Game (USA)"))
            hit = repo.find("Game (USA)", "dc")
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.zip_path.name, "redump_dc_cuesheets.zip")
            self.assertIsNone(repo.find("Game (USA)", "ps2"))


class VerificationEvidenceTests(unittest.TestCase):
    def test_result_without_checked_inputs_is_not_cached(self):
        cache = VerificationCache()
        self.addCleanup(cache.close)
        with patch.object(cache, "request") as request:
            cache.remember_result(ResultRecord("Game", "Passed", "Done", source=Path("Game.iso")), VerificationMode.MANIFEST, Path("dats"))
        request.assert_not_called()

    def test_changed_dat_rejects_late_verified_result(self):
        from redump_engine.verification_cache import signature
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Game.iso"
            image.write_bytes(b"image")
            dats = root / "dats"
            dats.mkdir()
            dat = dats / "game.dat"
            dat.write_text("old DAT")
            checked = signature(image, VerificationMode.IMAGE, dats)
            assert checked is not None
            dat.write_text("updated DAT")
            cache = VerificationCache()
            self.addCleanup(cache.close)
            cache.request(image, dats, VerificationMode.IMAGE, "Disc image: Passed", checked_inputs=checked)
            deadline = time.monotonic() + 3
            while cache.saving and time.monotonic() < deadline:
                cache.drain()
                time.sleep(.01)
            self.assertFalse(cache.saving)
            self.assertNotIn(image, cache.labels)
            self.assertIsNone(cache.lookup(image, dats))

    def test_independent_checks_preserve_dat_match_and_fall_back_when_dat_changes(self):
        from redump_engine.verification_storage import pack_records, unpack_records
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Game.chd"
            image.write_bytes(b"image")
            dats = root / "dats"
            dats.mkdir()
            store = VerificationRecords()
            store.remember(image, VerificationMode.IMAGE, "Disc image: Passed", dats)
            store.remember(image, VerificationMode.CHD, "CHD: Passed", dats)
            self.assertEqual("Disc image: Passed", store.lookup(image, dats))
            record = store.records[str(image.resolve())]
            self.assertEqual({"image", "chd"}, set(record["checks"]))
            restored = VerificationRecords(unpack_records(json.loads(json.dumps(pack_records(store.records)))))
            self.assertEqual("Disc image: Passed", restored.lookup(image, dats))
            (dats / "updated.dat").write_text("new DAT")
            self.assertEqual("CHD: Passed", restored.lookup(image, dats))
            image.write_bytes(b"changed image")
            self.assertIsNone(restored.lookup(image, dats))

    def test_coalescing_retains_distinct_pending_check_modes(self):
        from redump_engine.verification_cache import signature
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Game.chd"
            image.write_bytes(b"image")
            dats = root / "dats"
            dats.mkdir()
            first, second = signature(image, VerificationMode.IMAGE, dats), signature(image, VerificationMode.CHD, dats)
            entered, release = threading.Event(), threading.Event()
            calls = 0
            actual = signature
            def blocked(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    self.assertTrue(release.wait(3))
                return actual(*args)
            cache = VerificationCache()
            self.addCleanup(cache.close)
            self.addCleanup(release.set)
            with patch("redump_engine.verification_cache.signature", side_effect=blocked):
                cache.request(image, dats, VerificationMode.IMAGE, "Disc image: Passed", checked_inputs=first)
                self.assertTrue(entered.wait(3))
                cache.request(image, dats, VerificationMode.CHD, "CHD: Passed", checked_inputs=second)
                release.set()
                deadline = time.monotonic() + 3
                while cache.saving and time.monotonic() < deadline:
                    cache.drain()
                    time.sleep(.01)
                self.assertFalse(cache.saving)
            self.assertEqual("Disc image: Passed", cache.labels[image])
            self.assertEqual({"image", "chd"}, set(cache.records[str(image)]["checks"]))
