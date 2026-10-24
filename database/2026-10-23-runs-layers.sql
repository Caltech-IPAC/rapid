-- The runs' settings layers (foundation plan v9, Part 1, amended 2026-10-23):
-- the resolved body's hash and the name and SHA-256 of each layer it was
-- resolved from at `run create`. schema.sql carries the column for a fresh
-- database; this file brings an existing one to it, applied once.
ALTER TABLE runs ADD COLUMN layers jsonb;
