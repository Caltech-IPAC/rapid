--------------------------------------------------------------------------------------------------------------------------
-- 20260928-01-drop-acceptances.sql
--
-- Drops the acceptances table (20260926-05). The promotion gate is "required checks pass"
-- only (checks.md §The promotion gate): a candidate whose required check failed is
-- replaced, or the policy is corrected by a new version, never accepted past the gate, so
-- nothing records or reads an acceptance any more. The table's rows go with it; the
-- promotions and checks they cited stay. A release built before this change reads the
-- table from `check show`, `run show` and the promotion walk, so its promotions fail once
-- this applies: promote under a release built after it. Idempotent (IF EXISTS), so the
-- file applies twice without error; dropping the table drops its index and grants.
--------------------------------------------------------------------------------------------------------------------------

DROP TABLE IF EXISTS acceptances;
