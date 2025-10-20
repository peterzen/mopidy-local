import logging
import pathlib
import time

from mopidy import commands
from mopidy.audio import scan, tags
from mopidy.models import Album, Artist, Track

from mopidy_local import cueparser, mtimes, storage, translator

logger = logging.getLogger(__name__)

MIN_DURATION_MS = 100  # Shortest length of track to include.


class LocalCommand(commands.Command):
    def __init__(self):
        super().__init__()
        self.add_child("scan", ScanCommand())
        self.add_child("clear", ClearCommand())


class ClearCommand(commands.Command):
    help = "Clear local media files from the local library."

    def run(self, args, config):
        library = storage.LocalStorageProvider(config)

        prompt = "Are you sure you want to clear the library? [y/N] "

        if input(prompt).lower() != "y":
            print("Clearing library aborted")
            return 0

        if library.clear():
            print("Library successfully cleared")
            return 0

        print("Unable to clear library")
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
            )
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
        
        # Scan CUE sheets after regular files
        self._scan_cue_sheets(
            media_dir=media_dir,
            file_mtimes=file_mtimes,
            library=library,
            flush_threshold=config["local"]["scan_flush_threshold"],
        )

        library.close()
        return 0

    def _find_files(self, *, media_dir, follow_symlinks):
        logger.info(f"Finding files in {media_dir.as_uri()} ...")
        file_mtimes, file_errors = mtimes.find_mtimes(
            media_dir, follow=follow_symlinks
        )
        logger.info(f"Found {len(file_mtimes)} files in {media_dir.as_uri()}")

        if file_errors:
            logger.warning(
                f"Encountered {len(file_errors)} errors "
                f"while finding files in {media_dir.as_uri()}"
            )
        for path in file_errors:
            logger.warning(f"Error for {path.as_uri()}: {file_errors[path]}")

        return file_mtimes

    def _check_tracks_in_library(
        self, *, media_dir, file_mtimes, library, force_rescan
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
            else:
                return False

        def _extension_filters(
            relative_path, file_uri, included_file_exts, excluded_file_exts
        ):
            if included_file_exts:
                if relative_path.suffix.lower() in included_file_exts:
                    logger.debug(
                        f"Added {file_uri}: File extension on included list"
                    )
                    return True
                else:
                    logger.debug(
                        f"Skipped {file_uri}: File extension not on included list"
                    )
                    return False
            else:
                if relative_path.suffix.lower() in excluded_file_exts:
                    logger.debug(
                        f"Skipped {file_uri}: File extension on excluded list"
                    )
                    return False
                else:
                    logger.debug(
                        f"Included {file_uri}: File extension not on excluded list"
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

        logger.info(
            f"Found {len(files_to_update)} tracks which need to be updated"
        )
        return files_to_update

    def _scan_metadata(
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
            try:
                file_uri = absolute_path.as_uri()
                result = scanner.scan(file_uri)

                if not result.playable:
                    logger.warning(
                        f"Failed scanning {file_uri}: No audio found in file"
                    )
                elif result.duration is None:
                    logger.warning(
                        f"Failed scanning {file_uri}: "
                        "No duration information found in file"
                    )
                elif result.duration < MIN_DURATION_MS:
                    logger.warning(
                        f"Failed scanning {file_uri}: "
                        f"Track shorter than {MIN_DURATION_MS}ms"
                    )
                else:
                    local_uri = translator.path_to_local_track_uri(
                        absolute_path, media_dir
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

    def _scan_cue_sheets(
        self, *, media_dir, file_mtimes, library, flush_threshold
    ):
        """Scan CUE sheet files and add virtual tracks."""
        logger.info("Scanning for CUE sheets...")
        
        # Find all .cue files
        cue_files = []
        for absolute_path in file_mtimes:
            if absolute_path.suffix.lower() == '.cue':
                cue_files.append(absolute_path)
        
        if not cue_files:
            logger.info("No CUE sheets found")
            return
        
        logger.info(f"Found {len(cue_files)} CUE sheets")
        progress = _ScanProgress(batch_size=flush_threshold, total=len(cue_files))
        scanner = scan.Scanner(10000)  # 10 second timeout for audio file scan
        
        for cue_path in sorted(cue_files):
            try:
                # Parse the CUE sheet
                cue_sheet = cueparser.parse_cue_sheet(cue_path)
                if not cue_sheet:
                    logger.warning("Failed to parse CUE sheet: %s", cue_path)
                    continue
                
                # Get the audio file
                audio_file = cue_sheet.get_audio_file()
                if not audio_file:
                    logger.warning(
                        "Skipping %s: No valid audio file found or multi-file CUE", cue_path
                    )
                    continue
                
                # Scan the audio file to get its duration
                try:
                    audio_uri = audio_file.as_uri()
                    result = scanner.scan(audio_uri)
                    
                    if not result.playable or result.duration is None:
                        logger.warning(
                            f"Skipping {cue_path}: Audio file not playable or no duration"
                        )
                        continue
                    
                    file_duration_ms = result.duration
                except Exception as e:
                    logger.warning(
                        "Failed to scan audio file for %s: %s", cue_path, e
                    )
                    continue
                
                # Generate virtual tracks
                mtime = file_mtimes.get(cue_path)
                for cue_track in cue_sheet.tracks:
                    # Use track end time or file duration for last track
                    end_ms = cue_track.end_ms
                    if end_ms is None:
                        end_ms = file_duration_ms
                    
                    # Create track metadata
                    track_artist = cue_track.performer or cue_sheet.performer
                    album_artist = cue_sheet.performer
                    
                    # Build artists
                    artists = []
                    if track_artist:
                        artist_uri = storage.model_uri(
                            "artist",
                            Artist(name=track_artist)
                        )
                        artists = [Artist(uri=artist_uri, name=track_artist)]
                    
                    # Build album
                    album = None
                    if cue_sheet.title:
                        album_artists = []
                        if album_artist:
                            albumartist_uri = storage.model_uri(
                                "artist",
                                Artist(name=album_artist)
                            )
                            album_artists = [
                                Artist(uri=albumartist_uri, name=album_artist)
                            ]
                        
                        album_uri = storage.model_uri(
                            "album",
                            Album(
                                name=cue_sheet.title,
                                artists=frozenset(album_artists) if album_artists else None,
                                num_tracks=len(cue_sheet.tracks),
                                date=cue_sheet.date,
                            )
                        )
                        album = Album(
                            uri=album_uri,
                            name=cue_sheet.title,
                            artists=frozenset(album_artists) if album_artists else None,
                            num_tracks=len(cue_sheet.tracks),
                            date=cue_sheet.date,
                        )
                    
                    # Create virtual track URI
                    # Use CUE path + track number to create unique URI
                    relative_cue = cue_path.relative_to(media_dir)
                    virtual_uri = f"local:track:{relative_cue}#track{cue_track.number}"
                    
                    # Build track
                    track = Track(
                        uri=virtual_uri,
                        name=cue_track.title or f"Track {cue_track.number}",
                        artists=frozenset(artists) if artists else None,
                        album=album,
                        track_no=cue_track.number,
                        genre=cue_sheet.genre,
                        date=cue_sheet.date,
                        length=end_ms - cue_track.start_ms,
                        comment=cue_sheet.comment,
                        last_modified=mtime,
                    )
                    
                    # Prepare CUE info for database
                    cue_info = {
                        "backing_file": str(audio_file.relative_to(media_dir)),
                        "start_ms": cue_track.start_ms,
                        "end_ms": end_ms,
                    }
                    
                    # Add to library
                    library.add(track, cue_info=cue_info)
                    logger.debug("Added virtual track: %s", track.uri)
                
            except Exception as e:
                logger.warning("Failed processing CUE sheet %s: %s", cue_path, e)
            
            if progress.increment():
                progress.log()
                if library.flush():
                    logger.debug("Progress flushed")
        
        progress.log()
        logger.info("Done scanning CUE sheets")


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
                f"Scanned {self.count} of {self.total} files in {duration:.3f}s."
            )
        else:
            remainder = duration / self.count * (self.total - self.count)
            logger.info(
                f"Scanned {self.count} of {self.total} files "
                f"in {duration:.3f}s, ~{remainder:.0f}s left"
            )
