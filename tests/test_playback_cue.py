"""Tests for playback provider with virtual tracks."""

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from mopidy.models import Album, Artist, Track

from mopidy_local import playback, schema


class PlaybackProviderTest(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.tmpdir = tempfile.mkdtemp()
        self.media_dir = pathlib.Path(self.tmpdir) / "music"
        self.media_dir.mkdir()
        self.data_dir = pathlib.Path(self.tmpdir) / "data"
        self.data_dir.mkdir()
        
        # Create database
        self.db_path = self.data_dir / "library.db"
        self.conn = schema.Connection(str(self.db_path))
        schema.load(self.conn)
        
    def test_translate_uri_regular_track(self):
        """Test URI translation for regular tracks."""
        # Create a regular track
        track = Track(
            uri="local:track:song.mp3",
            name="Regular Song",
            artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            album=Album(
                uri="local:album:md5:test",
                name="Test Album",
                artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            ),
            length=240000,
        )
        
        # Insert without CUE info (regular track)
        schema.insert_track(self.conn, track, cue_info=None)
        self.conn.commit()
        
        backend = self._mock_backend()
        provider = playback.LocalPlaybackProvider(mock.Mock(), backend)
        
        file_uri = provider.translate_uri("local:track:song.mp3")
        
        self.assertIsNotNone(file_uri)
        self.assertTrue(file_uri.startswith("file://"))
        self.assertNotIn("#t=", file_uri)
        
    def test_translate_uri_virtual_track(self):
        """Test URI translation for virtual tracks from CUE sheets."""
        # Create backing file
        backing_file = self.media_dir / "album.flac"
        backing_file.touch()
        
        # Create a virtual track
        track = Track(
            uri="local:track:album.cue#track1",
            name="Track 1",
            artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            album=Album(
                uri="local:album:md5:test",
                name="Test Album",
                artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            ),
            track_no=1,
            length=180000,  # 3 minutes
        )
        
        # Insert with CUE info (virtual track)
        cue_info = {
            "backing_file": "album.flac",
            "start_ms": 0,
            "end_ms": 180000,
        }
        schema.insert_track(self.conn, track, cue_info=cue_info)
        self.conn.commit()
        
        backend = self._mock_backend()
        provider = playback.LocalPlaybackProvider(mock.Mock(), backend)
        
        file_uri = provider.translate_uri("local:track:album.cue#track1")
        
        self.assertIsNotNone(file_uri)
        self.assertTrue(file_uri.startswith("file://"))
        self.assertIn("#t=0.000,180.000", file_uri)
        
    def test_translate_uri_virtual_track_mid_album(self):
        """Test URI translation for virtual track in middle of album."""
        # Create backing file
        backing_file = self.media_dir / "album.flac"
        backing_file.touch()
        
        # Create a virtual track (track 2, starts at 3:30)
        track = Track(
            uri="local:track:album.cue#track2",
            name="Track 2",
            artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            album=Album(
                uri="local:album:md5:test",
                name="Test Album",
                artists=[Artist(uri="local:artist:md5:test", name="Test Artist")],
            ),
            track_no=2,
            length=150000,  # 2.5 minutes
        )
        
        # Insert with CUE info (starts at 3:30, ends at 6:00)
        cue_info = {
            "backing_file": "album.flac",
            "start_ms": 210000,  # 3:30
            "end_ms": 360000,    # 6:00
        }
        schema.insert_track(self.conn, track, cue_info=cue_info)
        self.conn.commit()
        
        backend = self._mock_backend()
        provider = playback.LocalPlaybackProvider(mock.Mock(), backend)
        
        file_uri = provider.translate_uri("local:track:album.cue#track2")
        
        self.assertIsNotNone(file_uri)
        self.assertIn("#t=210.000,360.000", file_uri)
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_backend(self):
        backend = mock.Mock()
        backend.config = {
            "local": {
                "media_dir": str(self.media_dir),
            }
        }

        library = mock.Mock()
        library._connect.return_value = self.conn
        backend.library = library
        return backend


if __name__ == "__main__":
    unittest.main()
