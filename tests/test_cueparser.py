"""Tests for CUE sheet parsing."""

import pathlib
import tempfile
import unittest

from mopidy_local import cueparser


class CueParserTest(unittest.TestCase):
    def test_parse_simple_cue(self):
        """Test parsing a simple CUE sheet."""
        cue_content = '''
PERFORMER "Artist Name"
TITLE "Album Title"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track 1"
    PERFORMER "Artist Name"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track 2"
    INDEX 01 03:45:27
  TRACK 03 AUDIO
    TITLE "Track 3"
    INDEX 01 07:12:45
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            cue_path = pathlib.Path(tmpdir) / "test.cue"
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            
            assert sheet is not None
            assert sheet.performer == "Artist Name"
            assert sheet.title == "Album Title"
            assert len(sheet.files) == 1
            assert sheet.files[0] == "album.flac"
            assert len(sheet.tracks) == 3
            
            # Check track 1
            track1 = sheet.tracks[0]
            assert track1.number == 1
            assert track1.title == "Track 1"
            assert track1.performer == "Artist Name"
            assert track1.start_ms == 0
            # End should be start of track 2
            assert track1.end_ms == 225360  # 3:45.27 in ms
            
            # Check track 2
            track2 = sheet.tracks[1]
            assert track2.number == 2
            assert track2.title == "Track 2"
            assert track2.start_ms == 225360
            assert track2.end_ms == 432600  # 7:12.45 in ms
            
            # Check track 3
            track3 = sheet.tracks[2]
            assert track3.number == 3
            assert track3.title == "Track 3"
            assert track3.start_ms == 432600
            assert track3.end_ms is None  # Last track, no end time

    def test_parse_cue_with_pregap(self):
        """Test parsing CUE with INDEX 00 (pre-gap)."""
        cue_content = '''
TITLE "Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track 1"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track 2"
    INDEX 00 03:45:00
    INDEX 01 03:47:00
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            cue_path = pathlib.Path(tmpdir) / "test.cue"
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            
            assert sheet is not None
            assert len(sheet.tracks) == 2
            
            track1 = sheet.tracks[0]
            # Track 1 should end at INDEX 00 of track 2
            assert track1.end_ms == 225000  # 3:45.00
            
            track2 = sheet.tracks[1]
            # Track 2 should start at INDEX 01
            assert track2.start_ms == 227000  # 3:47.00

    def test_parse_time_conversion(self):
        """Test time format conversion."""
        # 00:00:00 = 0ms
        assert cueparser._parse_time("00:00:00") == 0
        
        # 01:30:00 = 90 seconds = 90000ms
        assert cueparser._parse_time("01:30:00") == 90000
        
        # 03:45:27 = 3*60 + 45 + 27/75 seconds
        # = 225 + 0.36 = 225.36 seconds = 225360ms
        assert cueparser._parse_time("03:45:27") == 225360
        
        # 00:00:75 = 1 second (75 frames = 1 second)
        assert cueparser._parse_time("00:00:75") == 1000

    def test_unquote(self):
        """Test quote removal."""
        assert cueparser._unquote('"quoted"') == "quoted"
        assert cueparser._unquote("'quoted'") == "quoted"
        assert cueparser._unquote('not quoted') == "not quoted"
        assert cueparser._unquote('  "quoted"  ') == "quoted"

    def test_parse_cue_with_rem_comments(self):
        """Test parsing CUE with REM comments."""
        cue_content = '''
REM DATE 2023
REM GENRE "Rock"
REM COMMENT "Test comment"
REM DISCID 12345678
TITLE "Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track 1"
    INDEX 01 00:00:00
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            cue_path = pathlib.Path(tmpdir) / "test.cue"
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            
            assert sheet is not None
            assert sheet.date == "2023"
            assert sheet.genre == "Rock"
            assert sheet.comment == "Test comment"
            assert sheet.disc_id == "12345678"

    def test_parse_cue_with_track_performer(self):
        """Test parsing CUE where tracks have different performers."""
        cue_content = '''
PERFORMER "Various Artists"
TITLE "Compilation"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track 1"
    PERFORMER "Artist 1"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track 2"
    PERFORMER "Artist 2"
    INDEX 01 03:00:00
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            cue_path = pathlib.Path(tmpdir) / "test.cue"
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            
            assert sheet is not None
            assert sheet.performer == "Various Artists"
            assert sheet.tracks[0].performer == "Artist 1"
            assert sheet.tracks[1].performer == "Artist 2"

    def test_parse_nonexistent_file(self):
        """Test parsing non-existent CUE file."""
        sheet = cueparser.parse_cue_sheet(pathlib.Path("/nonexistent/file.cue"))
        assert sheet is None

    def test_get_audio_file_single_file(self):
        """Test getting audio file from single-file CUE."""
        cue_content = '''
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = pathlib.Path(tmpdir)
            cue_path = tmppath / "test.cue"
            audio_path = tmppath / "album.flac"
            
            # Create the audio file
            audio_path.touch()
            
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            audio = sheet.get_audio_file()
            
            assert audio is not None
            assert audio.name == "album.flac"

    def test_get_audio_file_multi_file(self):
        """Test that multi-file CUE returns None."""
        cue_content = '''
FILE "disc1.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
FILE "disc2.flac" WAVE
  TRACK 02 AUDIO
    INDEX 01 00:00:00
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            cue_path = pathlib.Path(tmpdir) / "test.cue"
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            audio = sheet.get_audio_file()
            
            # Should return None for multi-file CUE
            assert audio is None

    def test_get_audio_file_missing(self):
        """Test getting audio file when it doesn't exist."""
        cue_content = '''
FILE "missing.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            cue_path = pathlib.Path(tmpdir) / "test.cue"
            with open(cue_path, "w") as f:
                f.write(cue_content)
            
            sheet = cueparser.parse_cue_sheet(cue_path)
            audio = sheet.get_audio_file()
            
            # Should return None if audio file doesn't exist
            assert audio is None
