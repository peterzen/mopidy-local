"""CUE sheet parser for virtual track support."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


@dataclass
class CueIndex:
    """Represents a CUE sheet index (typically INDEX 01)."""

    number: int
    minutes: int
    seconds: int
    frames: int

    @property
    def milliseconds(self) -> int:
        """Convert index time to milliseconds."""
        total_seconds = self.minutes * 60 + self.seconds + self.frames / 75
        return int(total_seconds * 1000)


@dataclass
class CueTrack:
    """Represents a track in a CUE sheet."""

    number: int
    title: str | None = None
    performer: str | None = None
    index: CueIndex | None = None


@dataclass
class CueSheet:
    """Represents a parsed CUE sheet."""

    file_path: Path
    title: str | None = None
    performer: str | None = None
    date: str | None = None
    genre: str | None = None
    audio_file: str | None = None
    tracks: list[CueTrack] | None = None

    def __post_init__(self):
        if self.tracks is None:
            self.tracks = []


def parse_cue_file(cue_path: Path) -> CueSheet | None:
    """
    Parse a CUE sheet file.

    Args:
        cue_path: Path to the .cue file

    Returns:
        CueSheet object or None if parsing fails
    """
    if not cue_path.exists():
        logger.warning("CUE file not found: %s", cue_path)
        return None

    try:
        # Try UTF-8 first, fall back to latin-1
        try:
            content = cue_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = cue_path.read_text(encoding="latin-1")

        return _parse_cue_content(cue_path, content)
    except Exception as e:
        logger.error("Failed to parse CUE file %s: %s", cue_path, e)
        return None


def _parse_cue_content(cue_path: Path, content: str) -> CueSheet:  # noqa: PLR0915, PLR0912, C901
    """Parse CUE file content."""
    cue = CueSheet(file_path=cue_path)
    current_track: CueTrack | None = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # REM comments can contain metadata
        if line.startswith("REM "):
            rem_match = re.match(r'REM\s+(\w+)\s+"?([^"]+)"?', line)
            if rem_match:
                key, value = rem_match.groups()
                key_lower = key.lower()
                if key_lower == "date":
                    cue.date = value.strip('"')
                elif key_lower == "genre":
                    cue.genre = value.strip('"')

        # TITLE
        elif line.startswith("TITLE "):
            title_match = re.match(r'TITLE\s+"([^"]+)"', line)
            if title_match:
                title = title_match.group(1)
                if current_track:
                    current_track.title = title
                else:
                    cue.title = title

        # PERFORMER
        elif line.startswith("PERFORMER "):
            perf_match = re.match(r'PERFORMER\s+"([^"]+)"', line)
            if perf_match:
                performer = perf_match.group(1)
                if current_track:
                    current_track.performer = performer
                else:
                    cue.performer = performer

        # FILE - audio file reference
        elif line.startswith("FILE "):
            file_match = re.match(r'FILE\s+"([^"]+)"\s+(\w+)', line)
            if file_match:
                filename = file_match.group(1)
                # Store only the first FILE entry (single-file CUE)
                if not cue.audio_file:
                    cue.audio_file = filename

        # TRACK
        elif line.startswith("TRACK "):
            track_match = re.match(r"TRACK\s+(\d+)\s+AUDIO", line)
            if track_match:
                track_num = int(track_match.group(1))
                current_track = CueTrack(number=track_num)
                cue.tracks.append(current_track)

        # INDEX
        elif line.startswith("INDEX ") and current_track:
            index_match = re.match(r"INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", line)
            if index_match:
                idx_num = int(index_match.group(1))
                # We primarily care about INDEX 01 (track start)
                if idx_num == 1:
                    minutes = int(index_match.group(2))
                    seconds = int(index_match.group(3))
                    frames = int(index_match.group(4))
                    current_track.index = CueIndex(
                        number=idx_num,
                        minutes=minutes,
                        seconds=seconds,
                        frames=frames,
                    )

    return cue


def find_cue_files(directory: Path) -> Iterator[Path]:
    """
    Find all .cue files in a directory.

    Args:
        directory: Directory to search

    Yields:
        Path objects for .cue files
    """
    if not directory.is_dir():
        return

    for cue_file in directory.glob("**/*.cue"):
        if cue_file.is_file():
            yield cue_file


def resolve_audio_file(cue: CueSheet) -> Path | None:
    """
    Resolve the audio file referenced by a CUE sheet.

    Args:
        cue: Parsed CUE sheet

    Returns:
        Path to the audio file or None if not found
    """
    if not cue.audio_file:
        return None

    # Audio file is relative to the CUE file location
    audio_path = cue.file_path.parent / cue.audio_file

    if audio_path.exists():
        return audio_path

    # Try case-insensitive match (for cross-platform compatibility)
    parent_dir = cue.file_path.parent
    for file in parent_dir.iterdir():
        if file.name.lower() == cue.audio_file.lower():
            return file

    logger.warning(
        "Audio file %s referenced by CUE %s not found",
        cue.audio_file,
        cue.file_path,
    )
    return None
