-- The datasets table (rapid_docs decisions, datasets-table; Russ Laher,
-- 2026-11-24) brought to an existing database, which keeps its rows.
-- schema.sql carries the table for a fresh database; this file brings an
-- existing one to it, applied once, in one transaction, as postgres, after
-- 2026-11-20-run-stopped.sql. It adds a table, which reaches the pipeline's
-- roles through the schema's default privileges, and a foreign key on runs.
--
-- Every dataset a run already names is registered first, so the foreign key
-- holds over every existing run and no run is changed; its description says
-- it was registered here from its runs. Then flight is registered where no
-- run named it, as schema.sql registers it. A second application fails at the
-- first statement (the table exists) and changes nothing.

BEGIN;

CREATE TABLE datasets (
    name text PRIMARY KEY,                           -- the dataset every scientific key carries
    description text NOT NULL,
    created timestamp with time zone DEFAULT now() NOT NULL
);

INSERT INTO datasets (name, description)
SELECT dataset, format('Registered by 2026-11-24-datasets.sql from its runs %s to %s', min(id), max(id))
FROM runs
GROUP BY dataset;

INSERT INTO datasets (name, description) VALUES
    ('flight', 'Flight, every program; the first, program 1049')
ON CONFLICT (name) DO NOTHING;

ALTER TABLE runs ADD FOREIGN KEY (dataset) REFERENCES datasets (name);

COMMIT;
