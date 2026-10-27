import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.cue import bin_files_from_cue
from redump_engine.file_io import write_metadata
from redump_engine.file_transaction import FileTransaction
from redump_engine.metadata import Manifest, ManifestDisc, load_dat_index, load_manifest, payload_files_from_paths, save_manifest
from redump_engine.workflow_organize import plan_organization


class UnifiedOrganizationTests(unittest.TestCase):
    def test_combined_cues_preserve_tracks_referenced_outside_the_collection(self):
        for external in (False, True):
            for fail in (False, True):
                with self.subTest(external=external, fail=fail), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    folder = root / "Collection"
                    folder.mkdir()
                    track = folder / "Shared.bin"
                    track.write_bytes(b"original shared track")
                    sources = [folder / "Game.cue", folder / "Alternate.cue"]
                    for source in sources:
                        source.write_text('FILE "Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
                    other = root / "Other.cue"
                    if external:
                        other.write_text('FILE "Collection/Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
                    save_manifest(folder / "Game.manifest.json", Manifest(1, "bin-cue", "Game", [
                        ManifestDisc(1, payload_files_from_paths([track], "bin"), [source.name for source in sources])]))
                    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
                    plan = plan_organization(sources[0], None)
                    self.assertEqual(2, sum(path.suffix == ".cue" for path in plan.moves))
                    self.assertEqual(external, track in plan.copies)
                    if fail:
                        write = write_metadata
                        def fail_manifest(path, content, target=plan.manifest, write=write):
                            if path == target:
                                raise OSError("publication failed")
                            return write(path, content)
                        with patch("redump_engine.workflow_layout.write_metadata", side_effect=fail_manifest):
                            with self.assertRaisesRegex(OSError, "publication failed"):
                                plan.apply()
                        self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
                    else:
                        plan.apply()
                        self.assertEqual(external, track.exists())
                        self.assertEqual(b"original shared track", (plan.folder / "Shared.bin").read_bytes())
                        if external:
                            self.assertTrue(all(path.is_file() for path in bin_files_from_cue(other)))
                        for source in sources:
                            self.assertTrue(all(path.is_file() for path in bin_files_from_cue(plan.folder / source.name)))

    def test_xiso_joins_original_with_identical_payload_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = Path(tmp) / "Microsoft - Xbox"
            destination = system / "Game (USA)"
            destination.mkdir(parents=True)
            original = destination / "Game (USA).iso"
            original.write_bytes(b"original")
            image = system / "Game (USA).xiso.iso"
            image.write_bytes(b"converted")
            payload = payload_files_from_paths([original], "iso")
            save_manifest(destination / "manifest.json", Manifest(1, "iso", "Game (USA)",
                          [ManifestDisc(1, payload, [original.name])], "xbox"))
            sidecar = system / "Game (USA).manifest.json"
            save_manifest(sidecar, Manifest(1, "iso", "Game (USA)",
                          [ManifestDisc(1, payload, [original.name, image.name])], "xbox"))
            plan_organization(sidecar, None).apply()
            combined = load_manifest(destination / "manifest.json")
            assert combined is not None
            self.assertEqual([original.name, image.name], combined.disc_entries[0].source_files)
            self.assertEqual(payload, combined.disc_entries[0].payload_files)
            self.assertEqual(b"converted", (destination / image.name).read_bytes())
            self.assertEqual(b"original", original.read_bytes())

    def test_cue_tracks_and_references_are_renamed_in_the_same_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, metadata, _, title = self.fixture(root)
            original = image.read_bytes()
            image.unlink()
            track = image.with_suffix(".bin")
            track.write_bytes(original)
            cue = image.with_suffix(".cue")
            cue.write_text(f'FILE "{track.name}" BINARY\n  TRACK 01 MODE1/2352\n')
            payload = payload_files_from_paths([track], "bin")
            save_manifest(metadata, Manifest(1, "bin-cue", "Old", [ManifestDisc(1, payload, [cue.name])], "psx"))
            (root / "dats" / "game.dat").write_text(f'<datafile><game name="{title}"><rom name="{title}.bin" '
                                                   f'size="{len(original)}" sha1="{payload[0]["sha1"]}" crc="{payload[0]["crc32"]}"/></game></datafile>')
            plan = plan_organization(cue, load_dat_index(root / "dats"))
            plan.apply()
            self.assertEqual((plan.folder / f"{title}.bin").read_bytes(), original)
            self.assertIn(f'FILE "{title}.bin"', (plan.folder / f"{title}.cue").read_text())

    def fixture(self, root: Path):
        system = root / "Sony - PlayStation 2"
        system.mkdir()
        image = system / "Old.iso"
        image.write_bytes(b"original disc")
        payload = payload_files_from_paths([image], "iso")
        metadata = system / "Old.manifest.json"
        save_manifest(metadata, Manifest(1, "iso", "Old", [ManifestDisc(1, payload, [image.name])], "ps2"))
        raw = json.loads(metadata.read_text())
        raw["custom"] = {"preserve": True}
        raw["discs"][0]["custom_disc"] = "keep"
        metadata.write_text(json.dumps(raw))
        dats = root / "dats"
        dats.mkdir()
        title = "Game (USA)"
        (dats / "game.dat").write_text(f'<datafile><game name="{title}"><rom name="{title}.iso" '
                                      f'size="{payload[0]["size"]}" sha1="{payload[0]["sha1"]}" crc="{payload[0]["crc32"]}"/></game></datafile>')
        return image, metadata, load_dat_index(dats), title

    def test_loose_image_gets_canonical_names_in_one_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, metadata, index, title = self.fixture(Path(tmp))
            plan = plan_organization(image, index)
            target = image.parent / title / f"{title}.iso"
            self.assertEqual(plan.moves[image], target)
            self.assertTrue(image.exists())
            plan.apply()
            self.assertEqual(target.read_bytes(), b"original disc")
            raw = json.loads(plan.manifest.read_text())
            self.assertEqual(raw["title"], title)
            self.assertEqual(raw["custom"], {"preserve": True})
            self.assertEqual(raw["discs"][0]["custom_disc"], "keep")
            self.assertFalse(metadata.exists())
            self.assertFalse(plan_organization(target, index).changed)

    def test_missing_dat_organizes_without_guessing_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, _, _, _ = self.fixture(Path(tmp))
            plan = plan_organization(image, None)
            self.assertEqual(plan.moves[image], image.parent / "Old" / "Old.iso")
            plan.apply()
            manifest = load_manifest(plan.manifest)
            assert manifest is not None
            self.assertEqual(manifest.title, "Old")

    def test_shared_manifest_retains_unselected_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, metadata, index, _ = self.fixture(Path(tmp))
            other = image.with_name("Other.iso")
            other.write_bytes(b"other game")
            manifest = load_manifest(metadata)
            assert manifest is not None
            manifest.disc_entries.append(ManifestDisc(2, payload_files_from_paths([other], "iso"), [other.name]))
            save_manifest(metadata, manifest)
            plan = plan_organization(image, index)
            plan.apply()
            self.assertTrue(other.exists())
            retained = load_manifest(metadata)
            assert retained is not None
            self.assertEqual([disc.disc for disc in retained.disc_entries], [2])

    def test_collisions_and_stale_preview_do_not_modify_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, _, index, title = self.fixture(Path(tmp))
            plan = plan_organization(image, index)
            image.write_bytes(b"changed after preview")
            with self.assertRaisesRegex(ValueError, "changed since planning"):
                plan.apply()
            self.assertTrue(image.exists())
            destination = image.parent / title
            destination.mkdir()
            (destination / f"{title}.iso").write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                plan_organization(image, index)

    def test_failure_rolls_back_move_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, metadata, index, _ = self.fixture(Path(tmp))
            plan = plan_organization(image, index)
            previous = metadata.read_bytes()
            with patch.object(FileTransaction, "transfer", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    plan.apply()
            self.assertTrue(image.exists())
            self.assertEqual(metadata.read_bytes(), previous)
            self.assertFalse(plan.folder.exists())
