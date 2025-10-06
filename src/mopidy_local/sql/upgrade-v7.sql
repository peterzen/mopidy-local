-- Mopidy-Local-SQLite schema upgrade v7 -> v8
-- Adds support for virtual tracks from CUE sheets

BEGIN EXCLUSIVE TRANSACTION;

-- Add new columns for virtual track support
ALTER TABLE track ADD COLUMN kind TEXT DEFAULT 'file';
ALTER TABLE track ADD COLUMN source TEXT DEFAULT 'fs';
ALTER TABLE track ADD COLUMN path TEXT;
ALTER TABLE track ADD COLUMN start_ms INTEGER;
ALTER TABLE track ADD COLUMN end_ms INTEGER;

-- Create indexes for virtual track queries
CREATE INDEX idx_track_kind ON track (kind);
CREATE INDEX idx_track_source ON track (source);
CREATE INDEX idx_track_path ON track (path);

-- Update existing tracks to have default values
UPDATE track SET kind = 'file', source = 'fs' WHERE kind IS NULL;

PRAGMA user_version = 8;  -- update schema version

END TRANSACTION;
