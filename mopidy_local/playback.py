import logging
import pathlib
from typing import Optional

from mopidy import backend

logger = logging.getLogger(__name__)

__all__ = ["LocalPlaybackProvider"]

try:
    from gi.repository import GLib
except ImportError:
    GLib = None
    logger.warning("GLib not available, virtual track monitoring disabled")


class LocalPlaybackProvider(backend.PlaybackProvider):
    def __init__(self, audio, backend):
        super().__init__(audio, backend)
        self._monitor_timer_id = None
        self._monitor_interval_ms = 0
        self._monitor_fast_deadline_monotonic = None
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
        # Last position we reported to core/clients (already normalized)
        self._last_virtual_position_ms = 0
        # Track if we've locally emitted EOS to avoid duplicates
        self._virtual_eos_emitted = False

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
        self._last_virtual_position_ms = 0
        self._virtual_eos_emitted = False

        # ⬇️ Local import to avoid circular import with actor.py
        from mopidy_local import translator

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
        self._last_virtual_position_ms = 0
        self._virtual_eos_emitted = False

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

        # Fast tick for up to 3 seconds while we wait to complete the initial seek
        self._monitor_fast_deadline_monotonic = (
            GLib.get_monotonic_time() + 3_000_000
        )  # µs
        self._monitor_interval_ms = 50
        self._monitor_timer_id = GLib.timeout_add(
            self._monitor_interval_ms, self._check_playback_position
        )
        self._virtual_eos_emitted = False
        logger.debug(
            "Started monitor: fast=%dms until seek completes (end=%dms)",
            self._monitor_interval_ms,
            self._current_virtual_track_end_ms,
        )

    def _stop_monitor_timer(self):
        """Stop the position monitoring timer."""
        if self._monitor_timer_id is not None and GLib is not None:
            try:
                GLib.source_remove(self._monitor_timer_id)
            except Exception:  # noqa: BLE001
                pass
        self._monitor_timer_id = None
        self._monitor_fast_deadline_monotonic = None
        logger.debug("Stopped position monitor")

    def _virtual_relative_position(
        self, absolute_ms: Optional[int]
    ) -> Optional[int]:
        """Convert absolute pipeline position to virtual-track-relative ms."""
        if absolute_ms is None:
            return None

        start_ms = self._current_virtual_track_start_ms
        if start_ms is None:
            # Not a virtual track; position already relative.
            return max(0, int(absolute_ms))

        try:
            relative = int(absolute_ms) - int(start_ms)
        except Exception:
            # Defensive: fall back to zero if conversion fails
            return 0

        if relative < 0:
            # During preroll/seek, keep position clipped at 0.
            return 0

        end_ms = self._current_virtual_track_end_ms
        if end_ms is not None:
            try:
                virtual_length = max(0, int(end_ms) - int(start_ms))
            except Exception:
                virtual_length = None
            if virtual_length is not None:
                relative = min(relative, virtual_length)

        return relative

    def _check_playback_position(self):
        """Return True to keep the timer, False to stop it."""
        try:
            # One actor round-trip per tick
            pos_future = self.audio.get_position()
            position_ms = pos_future.get()  # may be None during preroll

            # Handle pending seek first
            if self._seek_pending:
                # Try the seek on every tick until it sticks.
                self._perform_virtual_track_seek()
                if not self._seek_pending:
                    # switch to slower cadence immediately
                    self._stop_monitor_timer()
                    self._monitor_interval_ms = 1000
                    self._monitor_timer_id = GLib.timeout_add(
                        self._monitor_interval_ms,
                        self._check_playback_position,
                    )
                    logger.debug(
                        "Seek done; monitoring every %dms",
                        self._monitor_interval_ms,
                    )
                    return False  # the old fast timer is gone

                # Give preroll some time; avoid hammering if audio loop is busy.
                if position_ms is None:
                    # Cap the fast phase to ~3s
                    if GLib.get_monotonic_time() > (
                        self._monitor_fast_deadline_monotonic or 0
                    ):
                        logger.debug(
                            "Seek still pending; switching to 250ms cadence to reduce load"
                        )
                        self._stop_monitor_timer()
                        self._monitor_interval_ms = 250
                        self._monitor_timer_id = GLib.timeout_add(
                            self._monitor_interval_ms,
                            self._check_playback_position,
                        )
                        return False
                return True  # keep polling

            # From here: ACTIVE monitoring
            if position_ms is None:
                return True  # playback not started or pause; keep timer

            normalized = self._virtual_relative_position(position_ms)
            if normalized is not None:
                self._last_virtual_position_ms = normalized

            # Kick off fade once (no blocking ops here)
            if (
                not self._fade_active
                and self._fade_in_ms > 0
                and self._current_virtual_track_start_ms is not None
            ):
                self._start_volume_fade()

            end_ms = self._current_virtual_track_end_ms
            if end_ms is None:
                # No longer a virtual track; stop timer
                self._monitor_timer_id = None
                return False

            # Adaptive guard band: 2x our interval
            guard = max(100, 2 * self._monitor_interval_ms)
            if (
                not self._virtual_eos_emitted
                and position_ms >= (end_ms - guard)
            ):
                logger.info(
                    "Virtual boundary hit: pos=%d end=%d (guard=%d) -> EOS",
                    position_ms,
                    end_ms,
                    guard,
                )
                try:
                    self._virtual_eos_emitted = True
                    self.audio.emit_end_of_stream()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("emit_end_of_stream failed: %s", exc)

                # Clear state and stop; next track will start its own monitor
                self._current_virtual_track_end_ms = None
                self._current_virtual_track_start_ms = None
                self._monitor_timer_id = None
                return False

            return True

        except Exception as exc:  # noqa: BLE001
            logger.warning("Error in position monitor: %s", exc)
            return True

    def get_time_position(self):  # noqa: D401 - inherited docstring
        """Get current playback position, normalized for virtual tracks."""
        try:
            position_ms = self.audio.get_position().get()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Falling back to last known virtual position after error: %s", exc
            )
            return self._last_virtual_position_ms

        if position_ms is None:
            return self._last_virtual_position_ms

        normalized = self._virtual_relative_position(position_ms)
        if normalized is None:
            return self._last_virtual_position_ms

        self._last_virtual_position_ms = normalized
        return normalized

    def seek(self, time_position: int) -> bool:
        """Seek within the virtual track, translating to absolute position."""
        if time_position is None:
            return False

        start_ms = self._current_virtual_track_start_ms
        if start_ms is None:
            return self.audio.set_position(time_position).get()

        try:
            requested = max(0, int(time_position))
        except Exception:
            logger.warning("Invalid seek request %r; ignoring", time_position)
            return False

        absolute = int(start_ms) + requested
        end_ms = self._current_virtual_track_end_ms
        if end_ms is not None:
            try:
                absolute = min(absolute, int(end_ms))
            except Exception:
                pass

        try:
            success = self.audio.set_position(absolute).get()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error seeking within virtual track: %s", exc)
            return False

        if success:
            self._seek_pending = False
            self._last_virtual_position_ms = self._virtual_relative_position(
                absolute
            ) or 0
            self._virtual_eos_emitted = False
        return success

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
                self._last_virtual_position_ms = 0
                self._virtual_eos_emitted = False
            else:
                logger.warning("Seek to %dms failed", seek_start)
                # Don't clear _seek_pending, will try again next timer tick
        except Exception as exc:
            logger.warning("Error seeking to virtual track start: %s", exc)
            # Don't clear _seek_pending, will try again next timer tick

    def on_stop(self):
        """Called when playback stops."""
        self._stop_monitor_timer()
        self._monitor_interval_ms = 0
        self._current_virtual_track_end_ms = None
        self._current_virtual_track_start_ms = None
        self._seek_pending = False
        self._cancel_fade_timer()
        self._last_virtual_position_ms = 0
        self._virtual_eos_emitted = False

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
                mute_result = self.audio.set_volume(0)
                if hasattr(mute_result, "get"):
                    mute_result.get()
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
                    final_result = self.audio.set_volume(
                        int(self._fade_target_volume)
                    )
                    if hasattr(final_result, "get"):
                        final_result.get()
                finally:
                    self._cancel_fade_timer()
                return False

            # Intermediate step
            fraction = self._fade_step_index / float(self._fade_steps_total)
            new_vol = int(round(self._fade_target_volume * fraction))
            try:
                vol_result = self.audio.set_volume(new_vol)
                if hasattr(vol_result, "get"):
                    vol_result.get()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            # On any error, stop trying to fade
            self._cancel_fade_timer()
            return False
