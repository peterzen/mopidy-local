import logging
import pathlib
from typing import Optional

from mopidy import backend

from mopidy_local import translator

logger = logging.getLogger(__name__)

try:
    from gi.repository import GLib
except ImportError:
    GLib = None
    logger.warning("GLib not available, virtual track monitoring disabled")


class LocalPlaybackProvider(backend.PlaybackProvider):
    def __init__(self, audio, backend):
        super().__init__(audio, backend)
        self._monitor_timer_id = None
        self._current_virtual_track_end_ms = None
        self._current_virtual_track_start_ms = None
        self._seek_pending = False

    def translate_uri(self, uri):
        """Translate local URI to file URI, adding time fragment for virtual tracks."""
        if not uri.startswith("local:track:"):
            return None

        logger.debug("Translating URI %s", uri)

        # Stop any existing timer
        self._stop_monitor_timer()

        fragment_uri = self._translate_virtual_track(uri)
        if fragment_uri:
            logger.info("Using virtual-track translation: %s → %s", uri, fragment_uri)
            # Start monitoring for virtual tracks (this will also handle the seek)
            self._start_monitor_timer()
            return fragment_uri

        # Clear virtual track state for regular tracks
        self._current_virtual_track_start_ms = None
        self._current_virtual_track_end_ms = None
        self._seek_pending = False
        fallback_uri = translator.local_uri_to_file_uri(
            uri, self.backend.config["local"]["media_dir"]
        )
        logger.debug("Translated regular track %s to %s", uri, fallback_uri)
        return fallback_uri

    def _translate_virtual_track(self, uri: str) -> Optional[str]:
        """Return a playback URI for a virtual track or ``None`` if not virtual."""
        try:
            with self.backend.library._connect() as connection:  # noqa: SLF001
                row = connection.execute(
                    """
                    SELECT kind, backing_file, start_ms, end_ms
                      FROM track
                     WHERE uri = ?
                    """,
                    (uri,),
                ).fetchone()
        except Exception as exc:
            logger.warning("Error looking up virtual track %s: %s", uri, exc)
            return None

        if not row or row[0] != "virtual":
            logger.debug("Track %s is not marked virtual (row=%r)", uri, row)
            return None

        _, backing_file, start_ms, end_ms = row
        if not backing_file:
            logger.warning(
                "Virtual track %s missing backing file information", uri
            )
            return None

        media_dir = pathlib.Path(self.backend.config["local"]["media_dir"])
        backing_path = pathlib.Path(backing_file)
        if not backing_path.is_absolute():
            backing_path = media_dir / backing_path
        logger.debug(
            "Virtual track %s backed by %s (absolute %s)",
            uri,
            backing_file,
            backing_path,
        )

        try:
            file_uri = backing_path.as_uri()
        except ValueError as exc:
            logger.warning(
                "Invalid backing path for virtual track %s: %s", uri, exc
            )
            return None

        # Store start and end times for manual seeking and monitoring
        self._current_virtual_track_start_ms = start_ms if start_ms is not None else 0
        self._current_virtual_track_end_ms = end_ms
        self._seek_pending = True
        
        logger.info(
            "Virtual track %s: file=%s, start=%sms, end=%sms",
            uri,
            file_uri,
            start_ms,
            end_ms,
        )
        
        # Return plain file URI - we'll seek manually
        return file_uri

    def _start_monitor_timer(self):
        """Start monitoring playback position for virtual track boundaries."""
        if GLib is None or self._current_virtual_track_end_ms is None:
            return

        # Check every 500ms for more responsive track changes
        self._monitor_timer_id = GLib.timeout_add(500, self._check_playback_position)
        logger.debug(
            "Started position monitor for virtual track (end=%dms)",
            self._current_virtual_track_end_ms,
        )

    def _stop_monitor_timer(self):
        """Stop the position monitoring timer."""
        if self._monitor_timer_id is not None and GLib is not None:
            GLib.source_remove(self._monitor_timer_id)
            self._monitor_timer_id = None
            logger.debug("Stopped position monitor")

    def _check_playback_position(self):
        """Check if playback has reached the virtual track boundary."""
        try:
            # Perform pending seek if needed
            if self._seek_pending:
                self._perform_virtual_track_seek()
            
            # Get current position from audio
            position_ms = self.audio.get_position().get()
            
            if position_ms is None:
                # Playback hasn't started yet or is stopped
                return True  # Continue monitoring

            if self._current_virtual_track_end_ms is None:
                # No virtual track active, stop monitoring
                self._monitor_timer_id = None
                return False

            # Check if we've reached or passed the end time
            # Use a small buffer (100ms) to ensure we don't miss the boundary
            if position_ms >= (self._current_virtual_track_end_ms - 100):
                logger.info(
                    "Virtual track boundary reached (pos=%dms, end=%dms), "
                    "triggering EOS",
                    position_ms,
                    self._current_virtual_track_end_ms,
                )
                # Trigger end of stream to advance to next track
                self.audio.emit_end_of_stream()
                # Stop monitoring, the next track will start its own timer
                self._monitor_timer_id = None
                self._current_virtual_track_end_ms = None
                self._current_virtual_track_start_ms = None
                return False  # Stop this timer

            # Continue monitoring
            return True

        except Exception as exc:
            logger.warning("Error checking playback position: %s", exc)
            # Continue monitoring despite errors
            return True

    def _perform_virtual_track_seek(self):
        """Seek to the start position of a virtual track."""
        if not self._seek_pending or self._current_virtual_track_start_ms is None:
            return
        
        try:
            logger.info(
                "Seeking to virtual track start position: %dms",
                self._current_virtual_track_start_ms,
            )
            # Seek to the start position
            success = self.audio.set_position(self._current_virtual_track_start_ms).get()
            if success:
                logger.debug("Seek to %dms successful", self._current_virtual_track_start_ms)
                self._seek_pending = False
            else:
                logger.warning("Seek to %dms failed", self._current_virtual_track_start_ms)
                # Don't clear _seek_pending, will try again next timer tick
        except Exception as exc:
            logger.warning("Error seeking to virtual track start: %s", exc)
            # Don't clear _seek_pending, will try again next timer tick

    def on_stop(self):
        """Called when playback stops."""
        self._stop_monitor_timer()
        self._current_virtual_track_end_ms = None
        self._current_virtual_track_start_ms = None
        self._seek_pending = False
