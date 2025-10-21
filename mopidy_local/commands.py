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
        
        cue_files = [
            path for path in file_mtimes if path.suffix.lower() == ".cue"
        ]
        
        if not cue_files:
            logger.info("No CUE sheets found")
            return
        
        logger.info("Found %d CUE sheets", len(cue_files))
        progress = _ScanProgress(batch_size=flush_threshold, total=len(cue_files))
        scanner = scan.Scanner(10000)  # 10 second timeout for audio file scan
        
        for cue_path in sorted(cue_files):
            try:
                cue_sheet = cueparser.parse_cue_sheet(cue_path)
                if not cue_sheet:
                    logger.warning("Failed to parse CUE sheet: %s", cue_path)
                    continue

                tracks_by_file: dict[str, list[cueparser.CueTrack]] = {}
                for cue_track in cue_sheet.tracks:
                    if not cue_track.file:
                        logger.debug(
                            "Skipping track %s in %s: no backing FILE entry",
                            cue_track.number,
                            cue_path,
                        )
                        continue
                    tracks_by_file.setdefault(cue_track.file, []).append(cue_track)

                multi_track_files = {
                    name: tracks
                    for name, tracks in tracks_by_file.items()
                    if len(tracks) >= 2
                }

                if not multi_track_files:
                    logger.debug(
                        "Skipping %s: requires at least two tracks sharing a backing file",
                        cue_path,
                    )
                    continue

                album = None
                if cue_sheet.title:
                    album_artists = None
                    if cue_sheet.performer:
                        albumartist_uri = storage.model_uri(
                            "artist", Artist(name=cue_sheet.performer)
                        )
                        album_artists = frozenset(
                            [Artist(uri=albumartist_uri, name=cue_sheet.performer)]
                        )
                    album_uri = storage.model_uri(
                        "album",
                        Album(
                            name=cue_sheet.title,
                            artists=album_artists,
                            num_tracks=len(cue_sheet.tracks),
                            date=cue_sheet.date,
                        ),
                    )
                    album = Album(
                        uri=album_uri,
                        name=cue_sheet.title,
                        artists=album_artists,
                        num_tracks=len(cue_sheet.tracks),
                        date=cue_sheet.date,
                    )

                try:
                    relative_cue = cue_path.relative_to(media_dir)
                except ValueError:
                    logger.warning(
                        "Skipping %s: CUE file not under media dir %s",
                        cue_path,
                        media_dir,
                    )
                    continue

                for file_name, cue_tracks in multi_track_files.items():
                    backing_path = cue_path.parent / file_name
                    if not backing_path.exists():
                        logger.warning(
                            "Skipping tracks in %s: backing file missing %s",
                            cue_path,
                            backing_path,
                        )
                        continue

                    try:
                        result = scanner.scan(backing_path.as_uri())
                    except Exception as error:
                        logger.warning(
                            "Failed to scan %s backing %s: %s",
                            cue_path,
                            backing_path,
                            error,
                        )
                        continue

                    if not result.playable or result.duration is None:
                        logger.warning(
                            "Skipping %s tracks in %s: audio not playable or duration unknown",
                            backing_path.name,
                            cue_path,
                        )
                        continue

                    file_duration_ms = result.duration

                    try:
                        backing_rel = backing_path.relative_to(media_dir)
                    except ValueError:
                        logger.warning(
                            "Skipping %s: backing file not under media dir %s",
                            backing_path,
                            media_dir,
                        )
                        continue

                    mtime = file_mtimes.get(cue_path)
                    for cue_track in cue_tracks:
                        start_ms = cue_track.start_ms or 0
                        end_ms = cue_track.end_ms or file_duration_ms
                        if end_ms < start_ms:
                            end_ms = start_ms

                        track_artist = cue_track.performer or cue_sheet.performer
                        artists = None
                        if track_artist:
                            artist_uri = storage.model_uri(
                                "artist", Artist(name=track_artist)
                            )
                            artists = frozenset(
                                [Artist(uri=artist_uri, name=track_artist)]
                            )

                        virtual_uri = (
                            f"local:track:{relative_cue}#track{cue_track.number}"
                        )
                        track = Track(
                            uri=virtual_uri,
                            name=cue_track.title or f"Track {cue_track.number}",
                            artists=artists,
                            album=album,
                            track_no=cue_track.number,
                            genre=cue_sheet.genre,
                            date=cue_sheet.date,
                            length=end_ms - start_ms,
                            comment=cue_sheet.comment,
                            last_modified=mtime,
                        )

                        cue_info = {
                            "backing_file": str(backing_rel),
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                        }

                        library.add(track, cue_info=cue_info)
                        logger.debug(
                            "Added virtual track %s backing %s", track.uri, backing_path
                        )
                
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
