-- The served loop (continuity design, exp/continuity): `loops`, one row per
-- `rapidpipe loop --serve`, at most one open per run. schema.sql carries it
-- for a fresh database; this file brings an existing one to it, applied once,
-- in one transaction, as postgres. The pipeline's roles reach the new table
-- through the schema's default privileges for tables postgres creates.

BEGIN;

-- A served loop (`rapidpipe loop --serve`): the loop's own arguments, which
-- `rapidpipe serve` runs one pass of each interval until the run drains. A run
-- has at most one open loop.
CREATE TABLE loops (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run bigint NOT NULL REFERENCES runs (id),
    instruction jsonb NOT NULL,                      -- the selection, job definition, queue and intake limit, as `loop` takes them
    opened_by text NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    passed_at timestamp with time zone,              -- its last pass; null before its first
    closed_at timestamp with time zone,
    exit_code integer,                               -- the verb's: 0 drained, 1 a job failed, 64 a first pass that admitted nothing
    CHECK ((closed_at IS NULL) = (exit_code IS NULL))
);

CREATE UNIQUE INDEX loops_one_open_per_run ON loops (run) WHERE closed_at IS NULL;

COMMIT;
