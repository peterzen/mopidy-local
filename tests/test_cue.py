"""Tests for CUE sheet parsing."""

import pathlib
import tempfile
import unittest

from mopidy_local import cue


class CueParserTest(unittest.TestCase):
    def test_parse_simple_cue(self):
        """Test parsing a simple CUE file."""
        cue_content = """REM DATE 2020
REM GENRE "Rock"
PERFORMER "Test Artist"
TITLE "Test Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track One"
    PERFORMER "Test Artist"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track Two"
    PERFORMER "Test Artist"
    INDEX 01 03:45:33
  TRACK 03 AUDIO
    TITLE "Track Three"
    INDEX 01 07:12:25
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cue", delete=False
        ) as f:
            f.write(cue_content)
            cue_path = pathlib.Path(f.name)

        try:
            cue_sheet = cue.parse_cue_file(cue_path)

            assert cue_sheet is not None
            assert cue_sheet.title == "Test Album"
            assert cue_sheet.performer == "Test Artist"
            assert cue_sheet.date == "2020"
            assert cue_sheet.genre == "Rock"
            assert cue_sheet.audio_file == "album.flac"
            assert len(cue_sheet.tracks) == 3

            # Test first track
            track1 = cue_sheet.tracks[0]
            assert track1.number == 1
            assert track1.title == "Track One"
            assert track1.performer == "Test Artist"
            assert track1.index is not None
            assert track1.index.milliseconds == 0

            # Test second track
            track2 = cue_sheet.tracks[1]
            assert track2.number == 2
            assert track2.title == "Track Two"
            # 3:45.33 = 3*60 + 45 + 33/75 = 225.44 seconds
            expected_ms = int((3 * 60 + 45 + 33 / 75) * 1000)
            assert abs(track2.index.milliseconds - expected_ms) < 10

            # Test third track
            track3 = cue_sheet.tracks[2]
            assert track3.number == 3
            assert track3.title == "Track Three"
            # Track performer inherits from album
            assert track3.performer is None

        finally:
            cue_path.unlink()

    def test_parse_cue_with_utf8(self):
        """Test parsing a CUE file with UTF-8 characters."""
        cue_content = """PERFORMER "Tëst Àrtist"
TITLE "Tëst Álbum"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Trâck Öne"
    INDEX 01 00:00:00
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cue", delete=False, encoding="utf-8"
        ) as f:
            f.write(cue_content)
            cue_path = pathlib.Path(f.name)

        try:
            cue_sheet = cue.parse_cue_file(cue_path)

            assert cue_sheet is not None
            assert cue_sheet.performer == "Tëst Àrtist"
            assert cue_sheet.title == "Tëst Álbum"
            assert len(cue_sheet.tracks) == 1
            assert cue_sheet.tracks[0].title == "Trâck Öne"

        finally:
            cue_path.unlink()

    def test_index_time_conversion(self):
        """Test INDEX time conversion to milliseconds."""
        idx = cue.CueIndex(number=1, minutes=1, seconds=30, frames=0)
        assert idx.milliseconds == 90000

        idx2 = cue.CueIndex(number=1, minutes=0, seconds=0, frames=75)
        assert idx2.milliseconds == 1000  # 75 frames = 1 second

        idx3 = cue.CueIndex(number=1, minutes=2, seconds=15, frames=37)
        # 2:15.37 = 135 + 37/75 seconds
        expected = int((135 + 37 / 75) * 1000)
        assert abs(idx3.milliseconds - expected) < 10

    def test_resolve_audio_file(self):
        """Test resolving audio file path from CUE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)

            # Create CUE file
            cue_path = tmpdir_path / "album.cue"
            cue_content = """FILE "album.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
"""
            cue_path.write_text(cue_content)

            # Create audio file
            audio_path = tmpdir_path / "album.flac"
            audio_path.write_bytes(b"fake audio data")

            # Parse and resolve
            cue_sheet = cue.parse_cue_file(cue_path)
            resolved = cue.resolve_audio_file(cue_sheet)

            assert resolved is not None
            assert resolved == audio_path

    def test_resolve_audio_file_case_insensitive(self):
        """Test case-insensitive audio file resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)

            # Create CUE file with lowercase reference
            cue_path = tmpdir_path / "album.cue"
            cue_content = """FILE "album.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
"""
            cue_path.write_text(cue_content)

            # Create audio file with uppercase name
            audio_path = tmpdir_path / "ALBUM.FLAC"
            audio_path.write_bytes(b"fake audio data")

            # Parse and resolve
            cue_sheet = cue.parse_cue_file(cue_path)
            resolved = cue.resolve_audio_file(cue_sheet)

            assert resolved is not None
            assert resolved.name.upper() == "ALBUM.FLAC"
