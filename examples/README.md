# CUE Sheet Examples

This directory contains example files demonstrating CUE sheet support in
Mopidy-Local.

## Files

### example.cue

A sample CUE sheet file showing the format and structure. This example
references a single FLAC file named `album.flac` and defines 4 tracks with
different timestamps.

### cue_demo.py

A demonstration script that shows how the CUE parser works. Run it with:

```bash
python examples/cue_demo.py
```

This script will:
1. Create a temporary CUE sheet
2. Parse it using the CUE parser
3. Display the album metadata and track information
4. Show what virtual track URIs would be generated

## Testing CUE Support

To test CUE support with real audio files:

1. Create a directory with a single audio file (e.g., `album.flac`)
2. Create a `.cue` file in the same directory referencing the audio file
3. Add the directory to your Mopidy media directory
4. Run `mopidy local scan`
5. The CUE tracks will appear as individual tracks in your library

## CUE Format Notes

- Each track needs an INDEX 01 timestamp
- Timestamps are in MM:SS:FF format (minutes:seconds:frames, where 75 frames = 1 second)
- The FILE entry should reference the audio file relative to the CUE file location
- Single-file CUE sheets only (one FILE entry)
