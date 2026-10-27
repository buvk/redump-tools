"""Same-named DAT entries from another console must not select CHD extraction."""
import hashlib
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import ChdInfo, Chdman
from redump_engine.cuesheets import CueRepository
from redump_engine.dat_models import DatEntry, DatIndex, DatRom
from redump_engine.filenames import normalize_name
from redump_engine.metadata import Manifest, ManifestDisc, load_manifest, save_manifest
from redump_engine.workflow_chd_verify import verify_chd_contents
from redump_engine.workflow_extract import ExtractWorkflow
from redump_engine.workflow_manifest import ManifestWorkflow

PAYLOAD = b"original PS2 DVD payload"
TITLE = "Shared Game (USA)"


class DvdOnlyTool(Chdman):
    def __init__(self):
        super().__init__(Path("unused"))

    def info(self, input_chd: Path):
        return ChdInfo(1000, 0, False, True, "")

    def extractdvd(self, input_chd: Path, output_file: Path):
        output_file.write_bytes(PAYLOAD)
        return ""

    def extractcd(self, input_chd: Path, output_file: Path):
        raise AssertionError("A DVD must not use CD extraction")

    def splitbin(self, input_chd: Path, output_cue: Path):
        raise AssertionError("A DVD must not use split BIN extraction")


class ChdSystemScopeTests(unittest.TestCase):
    def index(self):
        dc = DatEntry(TITLE, [DatRom(TITLE + ".bin", 1, "12345678", "a" * 40)], "dc")
        ps2 = DatEntry(TITLE, [DatRom(TITLE + ".iso", len(PAYLOAD), f"{zlib.crc32(PAYLOAD):08x}", hashlib.sha1(PAYLOAD).hexdigest())], "ps2")
        entries = [dc, ps2]  # The foreign entry is deliberately first.
        return DatIndex(entries, {normalize_name(TITLE): entries},
                        {rom.sha1: [entry] for entry in entries for rom in entry.roms if rom.sha1},
                        {rom.crc: [entry] for entry in entries for rom in entry.roms if rom.crc})

    def test_restoration_manifest_creation_and_contents_check_use_the_correct_system(self):
        for context in ("folder", "manifest", "metadata only"):
            for operation in ("restore", "manifest", "contents"):
                with self.subTest(context=context, operation=operation), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    folder = root / "Sony - PlayStation 2" / TITLE if context == "folder" else root / TITLE
                    folder.mkdir(parents=True)
                    source = folder / (TITLE + ".chd")
                    source.write_bytes(b"compressed DVD")
                    recorded = None
                    metadata = folder / "manifest.json"
                    if context == "manifest":
                        recorded = Manifest(1, "iso", TITLE, [ManifestDisc(1, [], [source.name])], "ps2")
                        save_manifest(metadata, recorded)
                    tool = DvdOnlyTool()
                    cues = CueRepository(root / "cues")
                    workflow = ManifestWorkflow(tool, self.index(), cues)
                    with patch.object(tool, "extractdvd", wraps=tool.extractdvd) as dvd:
                        if operation == "restore":
                            restore = ExtractWorkflow(tool, self.index(), cues, keep_sources=True, rename_folder=False)
                            reports = restore.process_game_dir(folder, selected_file=source, input_manifest=metadata if recorded else None)
                            self.assertEqual("iso", reports[0].decision)
                            self.assertEqual(PAYLOAD, reports[0].extracted_files[0].read_bytes())
                        elif operation == "manifest":
                            self.assertEqual("Completed", workflow.process_file(source)[0])
                            manifest = load_manifest(metadata)
                            assert manifest is not None
                            self.assertEqual("ps2", manifest.system)
                            self.assertEqual("iso", manifest.set_type)
                        else:
                            self.assertEqual("Passed", verify_chd_contents(source, workflow).status)
                        dvd.assert_called_once()
                    self.assertEqual(b"compressed DVD", source.read_bytes())

    def test_gd_and_dvd_metadata_take_precedence_over_name_hints(self):
        foreign = self.index().entries[0]
        self.assertEqual(("iso", False), ExtractWorkflow._info_decision(ChdInfo(1000, 0, False, True, ""), None, foreign))
        self.assertEqual(("bin-cue-split", True), ExtractWorkflow._info_decision(ChdInfo(1000, 2, True, False, "", True), None, None))
