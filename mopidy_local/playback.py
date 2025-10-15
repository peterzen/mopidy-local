import logging
import pathlib

from mopidy import backend

from mopidy_local import schema, translator

logger = logging.getLogger(__name__)


class LocalPlaybackProvider(backend.PlaybackProvider):
    def translate_uri(self, uri):
        """Translate local URI to file URI, adding time fragment for virtual tracks."""
        # First check if this is a virtual track
        try:
            from mopidy_local import Extension
            config = self.backend.config
            
            # Get database path
            data_dir = Extension.get_data_dir(config)
            dbpath = data_dir / "library.db"
            
            # Connect to database
            connection = schema.Connection(str(dbpath))
            
            # Query to get track info including virtual track fields
            cursor = connection.execute(
                """
                SELECT kind, backing_file, start_ms, end_ms 
                FROM track 
                WHERE uri = ?
                """,
                (uri,)
            )
            row = cursor.fetchone()
            connection.close()
            
            if row and row[0] == 'virtual':
                # This is a virtual track from a CUE sheet
                kind, backing_file, start_ms, end_ms = row
                
                # Get the media directory
                media_dir = pathlib.Path(config["local"]["media_dir"])
                
                # Construct the file URI for the backing file
                backing_path = media_dir / backing_file
                file_uri = backing_path.as_uri()
                
                # Add time fragment for GStreamer
                # Format: file:///path/to/file.flac#t=start_ms,end_ms
                # GStreamer expects seconds, so convert from ms
                start_sec = start_ms / 1000.0
                end_sec = end_ms / 1000.0
                
                # Add fragment to URI
                fragment_uri = f"{file_uri}#t={start_sec},{end_sec}"
                logger.debug(
                    f"Translated virtual track {uri} to {fragment_uri}"
                )
                return fragment_uri
            
        except Exception as e:
            logger.warning(f"Error checking for virtual track {uri}: {e}")
        
        # Fall back to normal translation for regular tracks
        return translator.local_uri_to_file_uri(
            uri, self.backend.config["local"]["media_dir"]
        )

