# CUE Sheet Support Implementation Summary

## Overview

This implementation adds full CUE sheet support to mopidy-local, allowing single-file albums with CUE sheets to appear as individual tracks in your library.

## What Changed

### Database Schema (v7 → v8)
- Added columns to `track` table for virtual track support:
  - `kind`: 'file' or 'virtual'
  - `source`: 'fs' or 'cue'
  - `backing_file`: Path to the actual audio file
  - `start_ms`, `end_ms`: Playback boundaries in milliseconds

### New Module: cueparser.py
- Pure Python CUE sheet parser (no external dependencies)
- Supports standard CUE format
- Handles track metadata, timing (INDEX 00/01), REM comments
- Rejects multi-file CUE sheets

### Scanner Enhancement (commands.py)
- After scanning audio files, scans for .cue files
- Parses each CUE and generates virtual track entries
- Each track gets proper metadata from the CUE sheet

### Playback Provider Enhancement (playback.py)
- Checks if track is virtual when translating URIs
- Adds `#t=start,end` fragment to file URIs for time-sliced playback
- GStreamer handles the actual seeking

## How It Works

1. **Scanning Phase**
   ```
   User runs: mopidy local scan
   
   Scanner finds:
   - /music/Album/album.flac (30 minutes)
   - /music/Album/album.cue (defines 10 tracks)
   
   Result: 10 virtual tracks added to database
   ```

2. **Playback Phase**
   ```
   User plays: Track 5 from Album
   
   translate_uri() returns:
   file:///music/Album/album.flac#t=210.0,255.0
   
   GStreamer:
   - Opens album.flac
   - Seeks to 3:30 (210 seconds)
   - Plays until 4:15 (255 seconds)
   ```

## Example CUE File

```cue
PERFORMER "Artist Name"
TITLE "Album Title"
REM DATE 2023
REM GENRE "Rock"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Song"
    PERFORMER "Artist Name"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Song"
    INDEX 01 03:30:27
  TRACK 03 AUDIO
    TITLE "Third Song"
    INDEX 01 07:15:00
```

## Testing

All tests pass (64 total):
- 48 original tests (unchanged)
- 10 CUE parser tests
- 3 integration tests
- 3 playback provider tests

Run tests:
```bash
pytest tests/test_cueparser.py -v
pytest tests/test_cue_integration.py -v
pytest tests/test_playback_cue.py -v
```

## Limitations

1. **Single-file only**: Multi-file CUE sheets are not supported
2. **Local files only**: No support for streaming sources
3. **Seekable required**: Backing audio file must support seeking
4. **No replaygain**: ReplayGain tags in CUE sheets are ignored

## Migration

When upgrading, the database will automatically migrate from v7 to v8:
- Existing tracks get `kind='file'`, `source='fs'`
- New columns added with appropriate defaults
- No data loss, fully backward compatible

## Code Changes Summary

Files modified:
- `mopidy_local/schema.py`: Schema v8, insert_track() extended
- `mopidy_local/storage.py`: add() method accepts cue_info parameter
- `mopidy_local/commands.py`: Added _scan_cue_sheets() method
- `mopidy_local/playback.py`: Enhanced translate_uri() for virtual tracks
- `mopidy_local/sql/upgrade-v7.sql`: Database migration script
- `README.rst`: Added CUE sheet documentation

Files added:
- `mopidy_local/cueparser.py`: CUE sheet parser (220 lines)
- `tests/test_cueparser.py`: Parser unit tests (230 lines)
- `tests/test_cue_integration.py`: Integration tests (160 lines)
- `tests/test_playback_cue.py`: Playback tests (180 lines)

Total new code: ~950 lines (including tests)
Total modified code: ~150 lines

## Architecture Diagram

```
Scan Phase:
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ .cue     │────▶│ Parser   │────▶│ Virtual  │────▶│ Database │
│ File     │     │          │     │ Tracks   │     │          │
└──────────┘     └──────────┘     └──────────┘     └──────────┘

Playback Phase:
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ User     │────▶│ Playback │────▶│ GStreamer│────▶│ Audio    │
│ Select   │     │ Provider │     │ + Seek   │     │ Output   │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
                      │
                      ▼
                 ┌──────────┐
                 │ Database │
                 │ (timing) │
                 └──────────┘
```
