-- dev's RuBR-AT real/bogus score and label on sources (rb-port): dev's
-- columns (rapidOpsSourcesTable.sql rb, migration 20261008 rblabel), written
-- by the difference job from SFFT's catalog (rapidpipe.science.difference.
-- realbogus). Nullable with no default, so the table is not rewritten:
-- rb is null where a source was not scored and on rows made before them,
-- rblabel 1 real, 0 bogus, -1 not scored, null not computed. schema.sql
-- carries them for a fresh database; this file brings an existing one to
-- it, applied once.
ALTER TABLE sources
    ADD COLUMN rb real,
    ADD COLUMN rblabel smallint;
