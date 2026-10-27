import tempfile
import unittest
from pathlib import Path

from test_hardening import FakeChdman

from redump_engine.chd_ops import ChdInfo
from redump_engine.cuesheets import CueRepository
from redump_engine.dat_loader import load_dat_index
from redump_engine.metadata import payload_files_from_paths
from redump_engine.workflow_chd_verify import verify_chd_contents
from redump_engine.workflow_manifest import ManifestWorkflow


class TrackChdman(FakeChdman):
    def __init__(self, tracks):
        super().__init__()
        self.tracks = tracks
        self.extracted = []

    def info(self, input_chd):
        return ChdInfo(sum(map(len, self.tracks)), len(self.tracks), True, False, "")

    def splitbin(self, input_chd, output_cue):
        cue = output_cue
        self.extracted.append(cue.parent)
        lines = []
        for number, data in enumerate(self.tracks, 1):
            track = cue.parent / f"Track {number}.bin"
            track.write_bytes(data)
            lines.append(f'FILE "{track.name}" BINARY\n TRACK {number:02d} AUDIO\n')
        cue.write_text("".join(lines))
        return ""


class ChdContentsTests(unittest.TestCase):
    def fixture(self, root, tracks):
        folder = root / "Sony - PlayStation" / "Game"
        folder.mkdir(parents=True)
        source = folder / "Game.chd"
        source.write_bytes(b"compressed")
        dats = root / "dats"
        dats.mkdir()
        roms = []
        for number, data in enumerate(tracks, 1):
            original = root / f"Track {number}.bin"
            original.write_bytes(data)
            payload = payload_files_from_paths([original], "bin")[0]
            roms.append(f'<rom name="Game (Track {number}).bin" size="{payload["size"]}" sha1="{payload["sha1"]}" crc="{payload["crc32"]}"/>')
        (dats / "game.dat").write_text('<datafile><header><name>Sony - PlayStation</name></header><game name="Game">' + "".join(roms) + '</game></datafile>')
        tool = TrackChdman(tracks)
        return source, tool, ManifestWorkflow(tool, load_dat_index(dats), CueRepository(root / "cues"))

    def test_multitrack_contents_match_without_writing_library_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, tool, workflow = self.fixture(Path(tmp), [b"data track", b"audio track"])
            before = list(source.parent.iterdir())
            self.assertEqual("Passed", verify_chd_contents(source, workflow).status)
            self.assertEqual(before, list(source.parent.iterdir()))
            self.assertEqual(b"compressed", source.read_bytes())
            self.assertTrue(tool.extracted)
            self.assertTrue(all(not folder.exists() for folder in tool.extracted))

    def test_missing_or_different_track_does_not_pass(self):
        for changed in ([b"data track"], [b"data track", b"different audio"]):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                source, tool, workflow = self.fixture(Path(tmp), [b"data track", b"audio track"])
                tool.tracks = changed
                self.assertEqual("Failed", verify_chd_contents(source, workflow).status)
                self.assertTrue(all(not folder.exists() for folder in tool.extracted))

    def test_dat_required_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, tool, workflow = self.fixture(Path(tmp), [b"data"])
            workflow.dat_index = None
            with self.assertRaisesRegex(ValueError, "DAT unavailable"):
                verify_chd_contents(source, workflow)
            self.assertEqual([], tool.extracted)
