import logging
import pathlib
import time

from mopidy import commands
from mopidy.audio import scan, tags

from mopidy_local import cue, mtimes, storage, translator

logger = logging.getLogger(__name__)

MIN_DURATION_MS = 100  # Shortest length of track to include.


class LocalCommand(commands.Command):
    def __init__(self):
        super().__init__()
        self.add_child("scan", ScanCommand())
        self.add_child("clear", ClearCommand())


class ClearCommand(commands.Command):
    help = "Clear local media files from the local library."

    def run(self, args, config):  # noqa: ARG002
        library = storage.LocalStorageProvider(config)

        prompt = "Are you sure you want to clear the library? [y/N] "

        if input(prompt).lower() != "y":
            print("Clearing library aborted")  # noqa: T201
            return 0

        if library.clear():
            print("Library successfully cleared")  # noqa: T201
            return 0

        print("Unable to clear library")  # noqa: T201
        return 1


class ScanCommand(commands.Command):
    help = "Scan local media files and populate the local library."

    def __init__(self):
        super().__init__()
        self.add_argument(
            "--limit",
            action="store",
            type=int,
            dest="limit",
            default=None,
            help="Maximum number of tracks to scan",
        )
        self.add_argument(
            "--force",
            action="store_true",
            dest="force",
            default=False,
            help="Force rescan of all media files",
        )

    def run(self, args, config):
        media_dir = pathlib.Path(config["local"]["media_dir"]).resolve()
        library = storage.LocalStorageProvider(config)

        file_mtimes = self._find_files(
            media_dir=media_dir,
            follow_symlinks=config["local"]["scan_follow_symlinks"],
        )

        files_to_update, files_in_library = self._check_tracks_in_library(
            media_dir=media_dir,
            file_mtimes=file_mtimes,
            library=library,
            force_rescan=args.force,
        )

        files_to_update.update(
            self._find_files_to_scan(
                media_dir=media_dir,
                file_mtimes=file_mtimes,
                files_in_library=files_in_library,
                included_file_exts=[
                    file_ext.lower()
                    for file_ext in config["local"]["included_file_extensions"]
                ],
                excluded_file_exts=[
                    file_ext.lower()
                    for file_ext in config["local"]["excluded_file_extensions"]
                ],
            ),
        )

        self._scan_metadata(
            media_dir=media_dir,
            file_mtimes=file_mtimes,
            files=files_to_update,
            library=library,
            timeout=config["local"]["scan_timeout"],
            flush_threshold=config["local"]["scan_flush_threshold"],
            limit=args.limit,
        )

        # Scan CUE files for virtual tracks
        self._scan_cue_files(
            media_dir=media_dir,
            library=library,
            file_mtimes=file_mtimes,
        )

        library.close()
        return 0

    def _find_files(self, *, media_dir, follow_symlinks):
        logger.info(f"Finding files in {media_dir.as_uri()} ...")
        file_mtimes, file_errors = mtimes.find_mtimes(media_dir, follow=follow_symlinks)
        logger.info(f"Found {len(file_mtimes)} files in {media_dir.as_uri()}")

        if file_errors:
            logger.warning(
                f"Encountered {len(file_errors)} errors "
                f"while finding files in {media_dir.as_uri()}",
            )
        for path in file_errors:
            logger.warning(f"Error for {path.as_uri()}: {file_errors[path]}")

        return file_mtimes

    def _check_tracks_in_library(
        self,
        *,
        media_dir,
        file_mtimes,
        library,
        force_rescan,
    ):
        num_tracks = library.load()
        logger.info(f"Checking {num_tracks} tracks from library")

        uris_to_remove = set()
        files_to_update = set()
        files_in_library = set()

        for track in library.begin():
            absolute_path = translator.local_uri_to_path(track.uri, media_dir)
            mtime = file_mtimes.get(absolute_path)
            if mtime is None:
                logger.debug(f"Removing {track.uri}: File not found")
                uris_to_remove.add(track.uri)
            elif mtime > track.last_modified or force_rescan:
                files_to_update.add(absolute_path)
            files_in_library.add(absolute_path)

        logger.info(f"Removing {len(uris_to_remove)} missing tracks")
        for local_uri in uris_to_remove:
            library.remove(local_uri)

        return files_to_update, files_in_library

    def _find_files_to_scan(
        self,
        *,
        media_dir,
        file_mtimes,
        files_in_library,
        included_file_exts,
        excluded_file_exts,
    ):
        files_to_update = set()

        def _is_hidden_file(relative_path, file_uri):
            if any(p.startswith(".") for p in relative_path.parts):
                logger.debug(f"Skipped {file_uri}: Hidden directory/file")
                return True
            return False

        def _extension_filters(
            relative_path,
            file_uri,
            included_file_exts,
            excluded_file_exts,
        ):
            if included_file_exts:
                if relative_path.suffix.lower() in included_file_exts:
                    logger.debug(f"Added {file_uri}: File extension on included list")
                    return True
                logger.debug(
                    f"Skipped {file_uri}: File extension not on included list",
                )
                return False
            if relative_path.suffix.lower() in excluded_file_exts:
                logger.debug(f"Skipped {file_uri}: File extension on excluded list")
                return False
            logger.debug(
                f"Included {file_uri}: File extension not on excluded list",
            )
            return True

        for absolute_path in file_mtimes:
            relative_path = absolute_path.relative_to(media_dir)
            file_uri = absolute_path.as_uri()

            if (
                not _is_hidden_file(relative_path, file_uri)
                and _extension_filters(
                    relative_path,
                    file_uri,
                    included_file_exts,
                    excluded_file_exts,
                )
                and absolute_path not in files_in_library
            ):
                files_to_update.add(absolute_path)

        logger.info(f"Found {len(files_to_update)} tracks which need to be updated")
        return files_to_update

    def _scan_metadata(  # noqa: PLR0913
        self,
        *,
        media_dir,
        file_mtimes,
        files,
        library,
        timeout,
        flush_threshold,
        limit,
    ):
        logger.info("Scanning...")

        files = sorted(files)[:limit]

        scanner = scan.Scanner(timeout)
        progress = _ScanProgress(batch_size=flush_threshold, total=len(files))

        for absolute_path in files:
            file_uri = absolute_path.as_uri()
            try:
                result = scanner.scan(file_uri)

                if not result.playable:
                    logger.warning(
                        f"Failed scanning {file_uri}: No audio found in file",
                    )
                elif result.duration is None:
                    logger.warning(
                        f"Failed scanning {file_uri}: "
                        "No duration information found in file",
                    )
                elif result.duration < MIN_DURATION_MS:
                    logger.warning(
                        f"Failed scanning {file_uri}: "
                        f"Track shorter than {MIN_DURATION_MS}ms",
                    )
                else:
                    local_uri = translator.path_to_local_track_uri(
                        absolute_path,
                        media_dir,
                    )
                    mtime = file_mtimes.get(absolute_path)
                    track = tags.convert_tags_to_track(result.tags).replace(
                        uri=local_uri,
                        length=result.duration,
                        last_modified=mtime,
                    )
                    library.add(track, result.tags, result.duration)
                    logger.debug(f"Added {track.uri}")
            except Exception as error:
                logger.warning(f"Failed scanning {file_uri}: {error}")

            if progress.increment():
                progress.log()
                if library.flush():
                    logger.debug("Progress flushed")

        progress.log()
        logger.info("Done scanning")

    def _scan_cue_files(self, *, media_dir, library, file_mtimes):
        """Scan CUE files and generate virtual tracks."""
        from mopidy.models import Album, Artist, Track

        logger.info("Scanning for CUE files...")
        cue_files = list(cue.find_cue_files(media_dir))
        logger.info(f"Found {len(cue_files)} CUE files")

        for cue_path in cue_files:
            try:
                cue_sheet = cue.parse_cue_file(cue_path)
                if not cue_sheet:
                    continue

                # Resolve the audio file
                audio_file = cue.resolve_audio_file(cue_sheet)
                if not audio_file or not audio_file.exists():
                    logger.warning(
                        f"Audio file for CUE {cue_path} not found, skipping"
                    )
                    continue

                # Get audio file duration
                scanner = scan.Scanner(timeout=5000)
                scan_result = scanner.scan(audio_file.as_uri())
                if not scan_result or not scan_result.duration:
                    logger.warning(
                        f"Could not determine duration for {audio_file}, skipping CUE"
                    )
                    continue

                total_duration_ms = scan_result.duration
                mtime = file_mtimes.get(cue_path, int(time.time() * 1000))

                # Process each track in the CUE
                for i, track_info in enumerate(cue_sheet.tracks):
                    if not track_info.index:
                        continue

                    start_ms = track_info.index.milliseconds
                    
                    # Determine end time
                    if i + 1 < len(cue_sheet.tracks):
                        next_track = cue_sheet.tracks[i + 1]
                        if next_track.index:
                            end_ms = next_track.index.milliseconds
                        else:
                            end_ms = total_duration_ms
                    else:
                        end_ms = total_duration_ms

                    # Calculate track length
                    track_length = end_ms - start_ms

                    # Generate virtual track URI
                    # Using format: local:track:cuesheet-<hash>-<track_num>
                    import hashlib
                    cue_hash = hashlib.md5(
                        str(cue_path).encode()
                    ).hexdigest()[:8]
                    virtual_uri = f"local:track:cuesheet-{cue_hash}-{track_info.number:02d}"

                    # Create artist
                    performer = (
                        track_info.performer
                        or cue_sheet.performer
                        or "Unknown Artist"
                    )
                    artist_uri = f"local:artist:cue-{hashlib.md5(performer.encode()).hexdigest()[:8]}"
                    artists = [
                        Artist(uri=artist_uri, name=performer)
                    ]

                    # Create album
                    album_name = cue_sheet.title or "Unknown Album"
                    album_uri = f"local:album:cue-{cue_hash}"
                    album = Album(
                        uri=album_uri,
                        name=album_name,
                        artists=frozenset(artists),
                        num_tracks=len(cue_sheet.tracks),
                        date=cue_sheet.date,
                    )

                    # Create track
                    track_title = track_info.title or f"Track {track_info.number}"
                    virtual_track = Track(
                        uri=virtual_uri,
                        name=track_title,
                        artists=frozenset(artists),
                        album=album,
                        track_no=track_info.number,
                        date=cue_sheet.date,
                        genre=cue_sheet.genre,
                        length=track_length,
                        last_modified=mtime,
                    )

                    # Add custom attributes for virtual track
                    virtual_track.kind = "virtual"
                    virtual_track.source = "cue"
                    virtual_track.path = str(audio_file)
                    virtual_track.start_ms = start_ms
                    virtual_track.end_ms = end_ms

                    # Add to library
                    library.add(virtual_track, {}, track_length)
                    logger.debug(f"Added virtual track {virtual_uri}")

                logger.info(f"Processed CUE file {cue_path.name} with {len(cue_sheet.tracks)} tracks")

            except Exception as error:
                logger.warning(f"Failed processing CUE file {cue_path}: {error}")

        logger.info("Done scanning CUE files")


class _ScanProgress:
    def __init__(self, *, batch_size, total):
        self.count = 0
        self.batch_size = batch_size
        self.total = total
        self.start = time.time()

    def increment(self):
        self.count += 1
        return self.batch_size and self.count % self.batch_size == 0

    def log(self):
        duration = time.time() - self.start
        if self.count >= self.total or not self.count:
            logger.info(
                f"Scanned {self.count} of {self.total} files in {duration:.3f}s.",
            )
        else:
            remainder = duration / self.count * (self.total - self.count)
            logger.info(
                f"Scanned {self.count} of {self.total} files "
                f"in {duration:.3f}s, ~{remainder:.0f}s left",
            )
