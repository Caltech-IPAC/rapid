-- dev's sumrat, nneg and nbad on sources (dev-columns): ZTF's candidate
-- metrics, computed by the difference job on a 5 x 5 stamp after a 64 px
-- local background (rapidpipe.science.difference.columns). Nullable: rows
-- published before them, and a source whose stamp leaves the image, are
-- null. schema.sql carries them for a fresh database; this file brings an
-- existing one to it, applied once.
ALTER TABLE sources
    ADD COLUMN sumrat real,
    ADD COLUMN nneg smallint,
    ADD COLUMN nbad smallint;
