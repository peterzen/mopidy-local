from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from mopidy import backend

from mopidy_local import translator

if TYPE_CHECKING:
    from mopidy.types import Uri

    from mopidy_local.actor import LocalBackend

logger = logging.getLogger(__name__)


class LocalPlaybackProvider(backend.PlaybackProvider):
    backend: LocalBackend

    def translate_uri(self, uri: Uri) -> Uri | None:
        # First check if this is a virtual track
        if uri.startswith("local:track:"):
            try:
                with self.backend.library._connect() as c:  # noqa: SLF001
                    # Get track metadata to check if it's virtual
                    rows = list(
                        c.execute(
                            """SELECT kind, path, start_ms, end_ms
                               FROM track WHERE uri = ?""",
                            (uri,),
                        )
                    )
                    if rows:
                        row = rows[0]
                        kind = row[0] if len(row) > 0 else "file"

                        if kind == "virtual":
                            # Virtual track: use path and add time fragment
                            path = row[1]
                            start_ms = row[2]
                            end_ms = row[3]

                            if path:
                                file_uri = Path(path).as_uri()

                                # Add Media Fragments URI time fragment
                                if start_ms is not None and end_ms is not None:
                                    start_s = start_ms / 1000
                                    end_s = end_ms / 1000
                                    file_uri += f"#t={start_s:.3f},{end_s:.3f}"
                                elif start_ms is not None:
                                    file_uri += f"#t={start_ms / 1000:.3f}"

                                return file_uri
            except Exception as e:
                logger.warning("Error looking up virtual track %s: %s", uri, e)

        # Regular track: use standard translation
        return translator.local_uri_to_file_uri(
            uri,
            self.backend.config["local"]["media_dir"],
        )
