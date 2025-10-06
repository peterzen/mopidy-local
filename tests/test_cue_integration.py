"""Integration tests for CUE sheet scanning."""

import pathlib
import tempfile
import unittest

from mopidy_local import cue


class CueIntegrationTest(unittest.TestCase):
    def test_end_to_end_cue_workflow(self):
        """Test the complete workflow: parse CUE, resolve audio, create tracks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)

            # Create a CUE file
            cue_content = """REM DATE 2021
REM GENRE Rock
PERFORMER "Test Band"
TITLE "Test Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Song"
    PERFORMER "Test Band"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Song"
    INDEX 01 03:30:45
  TRACK 03 AUDIO
    TITLE "Third Song"
    PERFORMER "Guest Artist"
    INDEX 01 07:15:30
"""
            cue_path = tmpdir_path / "album.cue"
            cue_path.write_text(cue_content)

            # Create a dummy audio file
            audio_path = tmpdir_path / "album.flac"
            audio_path.write_bytes(b"fake audio data")

            # Parse the CUE
            cue_sheet = cue.parse_cue_file(cue_path)
            assert cue_sheet is not None
            assert cue_sheet.title == "Test Album"
            assert cue_sheet.performer == "Test Band"
            assert cue_sheet.date == "2021"
            assert cue_sheet.genre == "Rock"
            assert len(cue_sheet.tracks) == 3

            # Resolve audio file
            resolved_audio = cue.resolve_audio_file(cue_sheet)
            assert resolved_audio is not None
            assert resolved_audio == audio_path

            # Check track 1
            track1 = cue_sheet.tracks[0]
            assert track1.number == 1
            assert track1.title == "First Song"
            assert track1.performer == "Test Band"
            assert track1.index.milliseconds == 0

            # Check track 2
            track2 = cue_sheet.tracks[1]
            assert track2.number == 2
            assert track2.title == "Second Song"
            # 3:30.45 = 210 + 45/75 = 210.6 seconds
            expected_ms = int((3 * 60 + 30 + 45 / 75) * 1000)
            assert abs(track2.index.milliseconds - expected_ms) < 10

            # Check track 3
            track3 = cue_sheet.tracks[2]
            assert track3.number == 3
            assert track3.title == "Third Song"
            assert track3.performer == "Guest Artist"
            # 7:15.30 = 435 + 30/75 = 435.4 seconds
            expected_ms = int((7 * 60 + 15 + 30 / 75) * 1000)
            assert abs(track3.index.milliseconds - expected_ms) < 10

    def test_find_cue_files_recursive(self):
        """Test finding CUE files in nested directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)

            # Create nested structure
            (tmpdir_path / "album1").mkdir()
            (tmpdir_path / "album1" / "disc1.cue").write_text("PERFORMER \"Test\"")

            (tmpdir_path / "album2" / "subdir").mkdir(parents=True)
            (tmpdir_path / "album2" / "subdir" / "album.cue").write_text(
                "PERFORMER \"Test\""
            )

            (tmpdir_path / "album3.cue").write_text("PERFORMER \"Test\"")

            # Find all CUE files
            cue_files = list(cue.find_cue_files(tmpdir_path))
            assert len(cue_files) == 3

            # Check that we found all of them
            cue_names = {f.name for f in cue_files}
            assert cue_names == {"disc1.cue", "album.cue", "album3.cue"}
