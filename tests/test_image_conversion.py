import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import Chdman
from redump_engine.cuesheets import CueRepository
from redump_engine.game_manifests import disc_for_source, merge_game_manifest
from redump_engine.image_conversion import convert_image
from redump_engine.library_metadata import manifest_for
from redump_engine.metadata import load_dat_index, load_manifest, payload_files_from_paths
from redump_engine.workflow_layout import LayoutPlan
from redump_engine.workflow_manifest import ManifestWorkflow


class ImageConversionTests(unittest.TestCase):
    def fixture(self, root, system, extension):
        folder = root / system / "Old title"
        folder.mkdir(parents=True)
        source = folder / "Old title.iso"
        source.write_bytes(b"original disc")
        output = folder / ("Old title" + extension)
        payload = payload_files_from_paths([source], "iso")[0]
        dats = root / "dats"
        dats.mkdir()
        (dats / "game.dat").write_text(f'<datafile><header><name>{system}</name></header><game name="Official (USA)">'
                                      f'<rom name="Official (USA).iso" size="{payload["size"]}" sha1="{payload["sha1"]}" '
                                      f'crc="{payload["crc32"]}"/></game></datafile>')
        manifests = ManifestWorkflow(Chdman(root / "tool.exe"), load_dat_index(dats), CueRepository(root / "cues"))

        def convert(managed):
            managed.process_file(source)
            metadata = manifest_for(source)
            assert metadata is not None
            manifest = load_manifest(metadata)
            assert manifest is not None
            disc = disc_for_source(source, manifest)
            assert disc is not None
            disc.source_files.append(output.name)
            merge_game_manifest(metadata, manifest)
            output.write_bytes(b"converted image")
            return "Completed", "Converted"
        return source, output, manifests, convert

    def test_redump_naming_and_retention_for_xiso_and_rvz(self):
        for system, extension in (("Microsoft - Xbox", ".xiso.iso"), ("Nintendo - GameCube", ".rvz")):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as tmp:
                source, output, manifests, convert = self.fixture(Path(tmp), system, extension)
                result = convert_image(source, output, manifests, convert, use_redump_names=True)
                self.assertEqual("Official (USA)", result.output.parent.name)
                self.assertEqual("Official (USA)" + extension, result.output.name)
                self.assertEqual(b"converted image", result.output.read_bytes())
                self.assertEqual(b"original disc", result.source.read_bytes())
                self.assertTrue((result.output.parent / "manifest.json").is_file())

    def test_overwrite_and_rename_remove_empty_old_folder_only_after_success(self):
        for system, extension in (("Microsoft - Xbox", ".xiso.iso"), ("Nintendo - GameCube", ".rvz")):
            for fail in (False, True):
                with self.subTest(system=system, fail=fail), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, output, manifests, convert = self.fixture(root, system, extension)
                    old_folder = source.parent
                    output.write_bytes(b"previous output")
                    before = {path: path.read_bytes() for path in old_folder.iterdir()}
                    apply = LayoutPlan.apply
                    def fail_after_apply(plan, *args, apply=apply, **kwargs):
                        apply(plan, *args, **kwargs)
                        raise OSError("late naming failure")
                    if fail:
                        with patch.object(LayoutPlan, "apply", fail_after_apply):
                            with self.assertRaisesRegex(OSError, "late naming failure"):
                                convert_image(source, output, manifests, convert, force=True, use_redump_names=True)
                        self.assertEqual(before, {path: path.read_bytes() for path in old_folder.iterdir()})
                        self.assertFalse(old_folder.with_name("Official (USA)").exists())
                    else:
                        result = convert_image(source, output, manifests, convert, force=True, use_redump_names=True)
                        self.assertEqual("Completed", result.status)
                        self.assertEqual(b"converted image", result.output.read_bytes())
                        self.assertEqual(b"original disc", result.source.read_bytes())
                        self.assertFalse(old_folder.exists())
                    self.assertFalse(list(root.rglob(".rollback_*")))

    def test_replacement_failure_restores_output_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output, manifests, convert = self.fixture(Path(tmp), "Microsoft - Xbox", ".xiso.iso")
            output.write_bytes(b"old output")
            manifests.process_file(source)
            metadata = manifest_for(source)
            assert metadata is not None
            original = metadata.read_bytes()
            def fail(managed):
                convert(managed)
                raise ValueError("failed conversion")
            with self.assertRaisesRegex(ValueError, "failed conversion"):
                convert_image(source, output, manifests, fail, force=True)
            self.assertEqual(b"old output", output.read_bytes())
            self.assertEqual(original, metadata.read_bytes())
            self.assertTrue(source.exists())

    def test_overwrite_is_opt_in_and_delete_happens_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output, manifests, convert = self.fixture(Path(tmp), "Nintendo - Wii", ".rvz")
            output.write_bytes(b"old output")
            with self.assertRaises(FileExistsError):
                convert_image(source, output, manifests, convert)
            result = convert_image(source, output, manifests, convert, force=True, keep_sources=False)
            self.assertEqual("Completed", result.status)
            self.assertFalse(source.exists())
            self.assertEqual(b"converted image", output.read_bytes())

    def test_missing_match_rolls_back_output_and_retains_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output, manifests, convert = self.fixture(Path(tmp), "Microsoft - Xbox", ".xiso.iso")
            source.write_bytes(b"different disc")
            with self.assertRaises(ValueError):
                convert_image(source, output, manifests, convert, use_redump_names=True, keep_sources=False)
            self.assertTrue(source.exists())
            self.assertFalse(output.exists())
