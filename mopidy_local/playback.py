import logging
import pathlib
from typing import Optional

from mopidy import backend

from mopidy_local import translator

logger = logging.getLogger(__name__)


class LocalPlaybackProvider(backend.PlaybackProvider):
    def translate_uri(self, uri):
        """Translate local URI to file URI, adding time fragment for virtual tracks."""
        if not uri.startswith("local:track:"):
            return None

        logger.debug("Translating URI %s", uri)

        fragment_uri = self._translate_virtual_track(uri)
        if fragment_uri:
            logger.debug("Using virtual-track translation for %s", uri)
            return fragment_uri

        fallback_uri = translator.local_uri_to_file_uri(
            uri, self.backend.config["local"]["media_dir"]
        )
        logger.debug("Translated regular track %s to %s", uri, fallback_uri)
        return fallback_uri

    def _translate_virtual_track(self, uri: str) -> Optional[str]:
        """Return a playback URI for a virtual track or ``None`` if not virtual."""
        try:
            connection = self.backend.library._connect()  # noqa: SLF001
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

        # GStreamer media fragments expect seconds.
        if start_ms is None:
            start_fragment = None
        else:
            start_fragment = f"{start_ms / 1000:.3f}"

        if end_ms is None:
            end_fragment = None
        else:
            end_fragment = f"{end_ms / 1000:.3f}"

        if start_fragment is None and end_fragment is None:
            logger.warning(
                "Virtual track %s missing start/end markers", uri
            )
            return file_uri

        if end_fragment is None:
            fragment = f"#t={start_fragment}"
        elif start_fragment is None:
            fragment = f"#t=0.000,{end_fragment}"
        else:
            fragment = f"#t={start_fragment},{end_fragment}"

        playback_uri = f"{file_uri}{fragment}"
        logger.debug(
            "Translated virtual track %s to %s (start=%s, end=%s)",
            uri,
            playback_uri,
            start_fragment,
            end_fragment,
        )
        return playback_uri
