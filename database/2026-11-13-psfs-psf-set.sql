-- The reference PSF's set on psfs (reference-psf-precomputed): a reference
-- PSF is imported as part of a named set, and the set is in its key, so
-- several sets are official side by side in one dataset; a science PSF has
-- none. The CHECK is added NOT VALID: the reference rows the psf stage wrote
-- until 2026-10-24 have no set and keep their rows and bindings as they are,
-- while every row written from this file on must satisfy it. A fresh
-- schema.sql database has the same CHECK validated. The view keeps the live
-- definition's columns, binding and run in place and appends psf_set (a
-- replace may only append; owner and grants stay), and its key match drops
-- the null set, so a science binding's key is unchanged. schema.sql carries
-- all of it for a fresh database; this file brings an existing one to it,
-- applied once, in one transaction, as postgres.

BEGIN;

ALTER TABLE psfs ADD COLUMN psf_set text;
ALTER TABLE psfs ADD CONSTRAINT psfs_check
    CHECK ((kind = 'reference') = (psf_set IS NOT NULL)) NOT VALID;
ALTER TABLE psfs DROP CONSTRAINT psfs_dataset_fid_sca_kind_job_key;
ALTER TABLE psfs ADD CONSTRAINT psfs_dataset_psf_set_fid_sca_kind_job_key
    UNIQUE NULLS NOT DISTINCT (dataset, psf_set, fid, sca, kind, job);

CREATE OR REPLACE VIEW psfs_official AS
SELECT t.psfid, t.job, t.dataset, t.fid, t.sca, t.kind, t.filename, t.checksum, t.status,
       t.created, o.id AS binding, j.run, t.psf_set
FROM psfs t
JOIN official o ON o.closed_at IS NULL AND o.product = 'psfs' AND o.job = t.job
    AND o.key = jsonb_strip_nulls(jsonb_build_object('dataset', t.dataset, 'psf_set', t.psf_set,
                                                     'fid', t.fid, 'sca', t.sca, 'kind', t.kind))
JOIN jobs j ON j.id = t.job;

COMMIT;
