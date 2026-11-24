-- SExtractor's position uncertainties on xsources (not in dev): the
-- windowed position's variances and covariance in pixel^2 and its error
-- ellipse on the sky, which the difference stage stores and the packet
-- stage converts to xErr, yErr, raErr and decErr. Nullable with no
-- default, so no table is rewritten; existing rows stay null (no
-- backfill), and their uncertainties in a packet are null. The columns
-- are added to xsources and, by inheritance, to every child table
-- xsources_<yyyymmdd>_<sca>; a child made afterwards copies them. The
-- xsources_official view on an existing database keeps the columns it was
-- made with and does not show these; the pipeline reads them through
-- xsources_of, whose row type is the table's. schema.sql carries the
-- columns for a fresh database; this file brings an existing one to it,
-- applied once, in one transaction, as postgres, with no difference or
-- packet job running. A second application fails at the statement (the
-- column exists) and changes nothing.

BEGIN;

ALTER TABLE xsources
    ADD COLUMN errx2win real,
    ADD COLUMN erry2win real,
    ADD COLUMN errxywin real,
    ADD COLUMN errawinworld real,
    ADD COLUMN errbwinworld real,
    ADD COLUMN errthetawinworld real;

COMMIT;
