#!/usr/bin/env python3
"""
Example script demonstrating CUE sheet parsing and virtual track creation.

This script shows how the CUE parser works and what virtual tracks look like.
"""

import tempfile
from pathlib import Path

from mopidy_local import cue


def main():
    # Create a sample CUE sheet
    cue_content = """REM DATE 2020
REM GENRE "Progressive Rock"
PERFORMER "Example Band"
TITLE "Example Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Opening Theme"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Main Song"
    PERFORMER "Example Band feat. Guest"
    INDEX 01 03:45:33
  TRACK 03 AUDIO
    TITLE "Finale"
    INDEX 01 08:12:25
"""

    # Write CUE to a temporary file
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        cue_path = tmpdir_path / "album.cue"
        cue_path.write_text(cue_content)

        # Create a fake audio file
        audio_path = tmpdir_path / "album.flac"
        audio_path.write_bytes(b"fake audio")

        # Parse the CUE
        print("=" * 60)
        print("CUE Sheet Parser Demo")
        print("=" * 60)

        cue_sheet = cue.parse_cue_file(cue_path)
        if not cue_sheet:
            print("Failed to parse CUE file")
            return

        print(f"\nAlbum: {cue_sheet.title}")
        print(f"Artist: {cue_sheet.performer}")
        print(f"Date: {cue_sheet.date}")
        print(f"Genre: {cue_sheet.genre}")
        print(f"Audio File: {cue_sheet.audio_file}")
        print(f"Number of Tracks: {len(cue_sheet.tracks)}")

        # Resolve audio file
        resolved_audio = cue.resolve_audio_file(cue_sheet)
        print(f"\nResolved Audio Path: {resolved_audio}")

        # Display track information
        print("\n" + "=" * 60)
        print("Virtual Tracks")
        print("=" * 60)

        for i, track in enumerate(cue_sheet.tracks):
            print(f"\nTrack {track.number}:")
            print(f"  Title: {track.title}")
            print(f"  Performer: {track.performer or cue_sheet.performer}")
            if track.index:
                print(f"  Start Time: {track.index.milliseconds}ms "
                      f"({track.index.minutes}:{track.index.seconds:02d}."
                      f"{track.index.frames:02d})")

                # Calculate duration
                if i + 1 < len(cue_sheet.tracks):
                    next_track = cue_sheet.tracks[i + 1]
                    if next_track.index:
                        duration_ms = (
                            next_track.index.milliseconds
                            - track.index.milliseconds
                        )
                        print(f"  Duration: {duration_ms}ms "
                              f"(~{duration_ms / 1000:.1f}s)")

        print("\n" + "=" * 60)
        print("In a real scenario, these would become virtual tracks in")
        print("the Mopidy database with URIs like:")
        print("  local:track:cuesheet-abc12345-01")
        print("  local:track:cuesheet-abc12345-02")
        print("  local:track:cuesheet-abc12345-03")
        print("=" * 60)


if __name__ == "__main__":
    main()
