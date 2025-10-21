-- Mopidy-Local-SQLite schema upgrade v7 -> v8
-- Adds support for CUE sheet virtual tracks

BEGIN EXCLUSIVE TRANSACTION;

-- Add columns to track table for virtual track support
ALTER TABLE track ADD COLUMN kind TEXT DEFAULT 'file';
ALTER TABLE track ADD COLUMN source TEXT DEFAULT 'fs';
ALTER TABLE track ADD COLUMN backing_file TEXT;
ALTER TABLE track ADD COLUMN start_ms INTEGER;
ALTER TABLE track ADD COLUMN end_ms INTEGER;

-- Create indexes for efficient virtual track queries
CREATE INDEX track_kind_index ON track (kind);
CREATE INDEX track_source_index ON track (source);
CREATE INDEX track_backing_file_index ON track (backing_file);

PRAGMA user_version = 8;  -- update schema version

END TRANSACTION;
