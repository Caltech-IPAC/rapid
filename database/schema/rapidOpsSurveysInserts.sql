--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSurveysInserts
--
-- Hand-maintained Surveys rows (ingest never writes this table).  Safe to re-run: existing
-- program_ids are skipped.
--------------------------------------------------------------------------------------------------------------------------


-- Simulations (program_id set by RAPIDDATASOURCE in pipeline/ingestL2Files.py).

INSERT INTO surveys (program_id, program_title, category) values (-1, 'SOCSIMS', 'SIM') ON CONFLICT (program_id) DO NOTHING;
INSERT INTO surveys (program_id, program_title, category) values (-2, 'OpenUniverse', 'SIM') ON CONFLICT (program_id) DO NOTHING;
INSERT INTO surveys (program_id, program_title, category) values (-3, 'RimTimSims', 'SIM') ON CONFLICT (program_id) DO NOTHING;


-- On-orbit programs.
