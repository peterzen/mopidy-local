"""Integration tests for CUE sheet support."""

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from mopidy_local import cueparser, schema, storage


class CueIntegrationTest(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.tmpdir = tempfile.mkdtemp()
        self.media_dir = pathlib.Path(self.tmpdir) / "music"
        self.media_dir.mkdir()
        self.data_dir = pathlib.Path(self.tmpdir) / "data"
        self.data_dir.mkdir()
        
    def test_virtual_track_storage(self):
        """Test storing and retrieving virtual tracks."""
        # Create a test database
        db_path = self.data_dir / "test.db"
        conn = schema.Connection(str(db_path))
        schema.load(conn)
        
        # Import necessary models
        from mopidy.models import Album, Artist, Track
        
        # Create a virtual track with CUE info
        track = Track(
            uri="local:track:album.cue#track1",
            name="Test Track",
            artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            album=Album(
                uri="local:album:md5:test",
                name="Test Album",
                artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            ),
            track_no=1,
            length=180000,  # 3 minutes
        )
        
        cue_info = {
            "backing_file": "album.flac",
            "start_ms": 0,
            "end_ms": 180000,
        }
        
        # Insert the track
        schema.insert_track(conn, track, cue_info=cue_info)
        conn.commit()
        
        # Retrieve and verify
        cursor = conn.execute(
            "SELECT kind, source, backing_file, start_ms, end_ms FROM track WHERE uri = ?",
            (track.uri,)
        )
        row = cursor.fetchone()
        
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "virtual")
        self.assertEqual(row[1], "cue")
        self.assertEqual(row[2], "album.flac")
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], 180000)
        
        conn.close()
    
    def test_regular_track_storage(self):
        """Test that regular tracks still work correctly."""
        # Create a test database
        db_path = self.data_dir / "test.db"
        conn = schema.Connection(str(db_path))
        schema.load(conn)
        
        # Import necessary models
        from mopidy.models import Album, Artist, Track
        
        # Create a regular track (no CUE info)
        track = Track(
            uri="local:track:song.mp3",
            name="Regular Song",
            artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            album=Album(
                uri="local:album:md5:test",
                name="Test Album",
                artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            ),
            track_no=1,
            length=240000,  # 4 minutes
        )
        
        # Insert without CUE info
        schema.insert_track(conn, track, cue_info=None)
        conn.commit()
        
        # Retrieve and verify
        cursor = conn.execute(
            "SELECT kind, source, backing_file, start_ms, end_ms FROM track WHERE uri = ?",
            (track.uri,)
        )
        row = cursor.fetchone()
        
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "file")
        self.assertEqual(row[1], "fs")
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])
        
        conn.close()
    
    def test_cue_parsing_integration(self):
        """Test full CUE parsing and track generation."""
        # Create a CUE file
        cue_content = '''
PERFORMER "Test Artist"
TITLE "Test Album"
REM DATE 2023
REM GENRE "Rock"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Track"
    PERFORMER "Test Artist"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Track"
    INDEX 01 03:30:00
  TRACK 03 AUDIO
    TITLE "Third Track"
    INDEX 01 07:15:00
'''
        cue_path = self.media_dir / "album.cue"
        with open(cue_path, "w") as f:
            f.write(cue_content)
        
        # Parse the CUE
        sheet = cueparser.parse_cue_sheet(cue_path)
        
        # Verify parsed data
        self.assertIsNotNone(sheet)
        self.assertEqual(sheet.performer, "Test Artist")
        self.assertEqual(sheet.title, "Test Album")
        self.assertEqual(sheet.date, "2023")
        self.assertEqual(sheet.genre, "Rock")
        self.assertEqual(len(sheet.tracks), 3)
        
        # Verify track 1
        self.assertEqual(sheet.tracks[0].title, "First Track")
        self.assertEqual(sheet.tracks[0].start_ms, 0)
        self.assertEqual(sheet.tracks[0].end_ms, 210000)  # 3:30
        
        # Verify track 2
        self.assertEqual(sheet.tracks[1].title, "Second Track")
        self.assertEqual(sheet.tracks[1].start_ms, 210000)  # 3:30
        self.assertEqual(sheet.tracks[1].end_ms, 435000)  # 7:15
        
        # Verify track 3
        self.assertEqual(sheet.tracks[2].title, "Third Track")
        self.assertEqual(sheet.tracks[2].start_ms, 435000)  # 7:15
        self.assertIsNone(sheet.tracks[2].end_ms)  # Last track

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
