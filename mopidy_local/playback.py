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
        # Fade-in configuration/state
        self._fade_in_ms = int(
            backend.config.get("local", {}).get("fade_in_ms", 0) or 0
        )
        self._fade_timer_id = None
        self._fade_steps_total = 0
        self._fade_step_index = 0
        self._fade_target_volume = None
        self._fade_active = False
        # Detect if audio exposes volume control; used for proper ramping.
        self._can_fade_via_volume = hasattr(self.audio, "set_volume") and hasattr(
            self.audio, "get_volume"
        )

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
            # Reset any prior fade state for a clean start
            self._cancel_fade_timer()
            return fragment_uri

        # Clear virtual track state for regular tracks
        self._current_virtual_track_start_ms = None
        self._current_virtual_track_end_ms = None
        self._seek_pending = False
        # No monitoring for regular tracks; ensure fade timers are clean.
        self._cancel_fade_timer()
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

        # Return plain file URI without time fragment
        # We'll handle seeking manually in the timer to avoid race conditions
        return file_uri

    def _start_monitor_timer(self):
        """Start monitoring playback position for virtual track boundaries."""
        if GLib is None or self._current_virtual_track_end_ms is None:
            return

        # Start with fast interval (50ms) for immediate seek, then slow down
        self._monitor_timer_id = GLib.timeout_add(50, self._check_playback_position)
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
            # Perform pending seek if needed (only once when playback is ready)
            if self._seek_pending:
                # Get current position to check if playback has started
                position_ms = self.audio.get_position().get()
                if position_ms is not None:
                    # Playback is ready, perform the seek
                    self._perform_virtual_track_seek()
                    # After successful seek, switch to slower monitoring interval
                    if not self._seek_pending:  # seek completed
                        self._stop_monitor_timer()
                        # Restart with slower interval for boundary monitoring
                        self._monitor_timer_id = GLib.timeout_add(500, self._check_playback_position)
                        logger.debug("Switched to 500ms monitoring interval after seek")
                # Continue fast polling until seek completes
                return True
            
            # Get current position from audio
            position_ms = self.audio.get_position().get()
            
            if position_ms is None:
                # Playback hasn't started yet or is stopped
                return True  # Continue monitoring
            
            # Trigger fade-in once playback is rolling
            if (
                not self._fade_active
                and self._fade_in_ms > 0
                and self._current_virtual_track_start_ms is not None
            ):
                self._start_volume_fade()

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
                # Use .get() to ensure the EOS signal is processed before we continue
                try:
                    self.audio.emit_end_of_stream().get()
                except Exception as e:
                    logger.warning("Failed to emit end of stream: %s", e)
                
                # Clear virtual track state
                self._current_virtual_track_end_ms = None
                self._current_virtual_track_start_ms = None
                # Stop monitoring, the next track will start its own timer
                self._monitor_timer_id = None
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
            # If we cannot perform a proper volume ramp, preroll slightly before the
            # intended start to reduce transients at segment boundaries.
            seek_start = self._current_virtual_track_start_ms
            if self._fade_in_ms > 0 and not self._can_fade_via_volume:
                preroll = min(self._fade_in_ms, seek_start)
                if preroll:
                    logger.debug(
                        "Applying preroll of %dms before start to reduce transients",
                        preroll,
                    )
                    seek_start -= preroll

            success = self.audio.set_position(seek_start).get()
            if success:
                logger.debug("Seek to %dms successful", seek_start)
                self._seek_pending = False
            else:
                logger.warning("Seek to %dms failed", seek_start)
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
        self._cancel_fade_timer()

    # --- Fade-in helpers -------------------------------------------------

    def _cancel_fade_timer(self):
        if self._fade_timer_id is not None and GLib is not None:
            GLib.source_remove(self._fade_timer_id)
        self._fade_timer_id = None
        self._fade_active = False
        self._fade_steps_total = 0
        self._fade_step_index = 0
        self._fade_target_volume = None

    def _start_volume_fade(self):
        """Attempt to ramp volume from 0 to current value over fade_in_ms.

        If audio volume control is not available, this is a no-op.
        """
        if GLib is None or self._fade_in_ms <= 0 or self._fade_active:
            return

        if not self._can_fade_via_volume:
            # Nothing to do; preroll seek handled in _perform_virtual_track_seek.
            return

        try:
            # Determine current target volume; fall back to 100 if unavailable.
            target = 100
            try:
                current = self.audio.get_volume().get()
                if isinstance(current, int) and 0 <= current <= 100:
                    target = current
            except Exception:  # noqa: BLE001
                pass

            # Start from 0, then ramp to target.
            try:
                self.audio.set_volume(0)
            except Exception:  # noqa: BLE001
                logger.debug("Audio.set_volume not available; skipping fade ramp")
                return

            # Configure timer/steps
            steps = max(4, min(20, int(self._fade_in_ms / 5)))
            interval = max(5, int(self._fade_in_ms / steps))

            self._fade_active = True
            self._fade_steps_total = steps
            self._fade_step_index = 0
            self._fade_target_volume = target

            logger.debug(
                "Starting volume fade-in: target=%d, steps=%d, interval=%dms",
                target,
                steps,
                interval,
            )

            # Schedule first step
            self._fade_timer_id = GLib.timeout_add(interval, self._fade_step)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unable to start fade-in: %s", exc)

    def _fade_step(self):
        """Timer callback to perform one fade step. Returns True to continue."""
        try:
            if not self._fade_active:
                return False

            self._fade_step_index += 1
            if self._fade_step_index >= self._fade_steps_total:
                # Final step; set to target and stop timer
                try:
                    self.audio.set_volume(int(self._fade_target_volume))
                finally:
                    self._cancel_fade_timer()
                return False

            # Intermediate step
            fraction = self._fade_step_index / float(self._fade_steps_total)
            new_vol = int(round(self._fade_target_volume * fraction))
            self.audio.set_volume(new_vol)
            return True
        except Exception:  # noqa: BLE001
            # On any error, stop trying to fade
            self._cancel_fade_timer()
            return False
