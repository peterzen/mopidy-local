"""CUE sheet parser for mopidy-local.

Implements a simple CUE sheet parser that extracts track information
from .cue files for single-file audio albums.
"""

import logging
import pathlib
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CueTrack:
    """Represents a single track from a CUE sheet."""

    def __init__(self, number: int):
        self.number = number
        self.title: Optional[str] = None
        self.performer: Optional[str] = None
        self.songwriter: Optional[str] = None
        self.start_ms: Optional[int] = None
        self.end_ms: Optional[int] = None
        self.index_00_ms: Optional[int] = None  # Pre-gap
        self.index_01_ms: Optional[int] = None  # Start of track


class CueSheet:
    """Represents a parsed CUE sheet."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.performer: Optional[str] = None
        self.title: Optional[str] = None
        self.songwriter: Optional[str] = None
        self.date: Optional[str] = None
        self.genre: Optional[str] = None
        self.catalog: Optional[str] = None
        self.disc_id: Optional[str] = None
        self.comment: Optional[str] = None
        self.files: List[str] = []
        self.tracks: List[CueTrack] = []

    def get_audio_file(self) -> Optional[pathlib.Path]:
        """Get the audio file referenced by this CUE sheet.
        
        Returns the first FILE entry from the CUE sheet, resolved relative
        to the CUE sheet's directory. Returns None for multi-file CUEs.
        """
        if len(self.files) != 1:
            return None
        
        # Resolve file path relative to CUE directory
        audio_file = self.path.parent / self.files[0]
        return audio_file if audio_file.exists() else None


def _parse_time(time_str: str) -> int:
    """Parse CUE time format MM:SS:FF to milliseconds.
    
    Args:
        time_str: Time in format MM:SS:FF where FF is frames (1/75th of a second)
    
    Returns:
        Time in milliseconds
    """
    match = re.match(r'(\d+):(\d+):(\d+)', time_str)
    if not match:
        return 0
    
    minutes, seconds, frames = map(int, match.groups())
    # Convert to milliseconds: MM*60*1000 + SS*1000 + FF*(1000/75)
    return minutes * 60000 + seconds * 1000 + int(frames * 1000 / 75)


def _unquote(s: str) -> str:
    """Remove surrounding quotes from a string."""
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def parse_cue_sheet(cue_path: pathlib.Path) -> Optional[CueSheet]:
    """Parse a CUE sheet file.
    
    Args:
        cue_path: Path to the .cue file
    
    Returns:
        CueSheet object if parsing succeeds, None otherwise
    """
    if not cue_path.exists():
        logger.warning("CUE file not found: %s", cue_path)
        return None
    
    try:
        # Try to detect encoding
        content = None
        for encoding in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
            try:
                with open(cue_path, 'r', encoding=encoding) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, LookupError):
                continue
        
        if content is None:
            logger.warning("Could not decode CUE file: %s", cue_path)
            return None
        
        return _parse_cue_content(cue_path, content)
    
    except Exception as e:
        logger.warning("Error parsing CUE file %s: %s", cue_path, e)
        return None


def _parse_cue_content(cue_path: pathlib.Path, content: str) -> CueSheet:
    """Parse the content of a CUE file."""
    sheet = CueSheet(cue_path)
    current_track: Optional[CueTrack] = None
    current_file: Optional[str] = None
    
    for line in content.splitlines():
        line = line.strip()
        
        # Skip empty lines and comments
        if not line or line.startswith('REM '):
            # Parse some useful REM commands
            if line.startswith('REM DATE '):
                sheet.date = line[9:].strip()
            elif line.startswith('REM GENRE '):
                sheet.genre = _unquote(line[10:].strip())
            elif line.startswith('REM COMMENT '):
                sheet.comment = _unquote(line[12:].strip())
            elif line.startswith('REM DISCID '):
                sheet.disc_id = line[11:].strip()
            continue
        
        # Parse commands
        if line.startswith('PERFORMER '):
            value = _unquote(line[10:])
            if current_track:
                current_track.performer = value
            else:
                sheet.performer = value
        
        elif line.startswith('TITLE '):
            value = _unquote(line[6:])
            if current_track:
                current_track.title = value
            else:
                sheet.title = value
        
        elif line.startswith('SONGWRITER '):
            value = _unquote(line[11:])
            if current_track:
                current_track.songwriter = value
            else:
                sheet.songwriter = value
        
        elif line.startswith('FILE '):
            # Extract filename (everything between first and last quote, or after FILE)
            match = re.match(r'FILE\s+"([^"]+)"\s+\w+', line) or \
                    re.match(r"FILE\s+'([^']+)'\s+\w+", line) or \
                    re.match(r'FILE\s+(\S+)\s+\w+', line)
            if match:
                current_file = match.group(1)
                sheet.files.append(current_file)
        
        elif line.startswith('TRACK '):
            # TRACK NN AUDIO
            match = re.match(r'TRACK\s+(\d+)\s+AUDIO', line)
            if match:
                track_num = int(match.group(1))
                current_track = CueTrack(track_num)
                sheet.tracks.append(current_track)
        
        elif line.startswith('INDEX '):
            # INDEX NN MM:SS:FF
            match = re.match(r'INDEX\s+(\d+)\s+([\d:]+)', line)
            if match and current_track:
                index_num = int(match.group(1))
                time_ms = _parse_time(match.group(2))
                
                if index_num == 0:
                    current_track.index_00_ms = time_ms
                elif index_num == 1:
                    current_track.index_01_ms = time_ms
        
        elif line.startswith('CATALOG '):
            sheet.catalog = line[8:].strip()
    
    # Calculate start and end times for each track
    for i, track in enumerate(sheet.tracks):
        # Use INDEX 01 as the start time (or INDEX 00 if 01 not present)
        track.start_ms = track.index_01_ms or track.index_00_ms or 0
        
        # End time is the start of the next track, or will be set to file duration
        if i + 1 < len(sheet.tracks):
            next_track = sheet.tracks[i + 1]
            # Use INDEX 00 of next track if available, otherwise INDEX 01
            track.end_ms = next_track.index_00_ms or next_track.index_01_ms
        # else: end_ms will be set to file duration during scanning
    
    return sheet
