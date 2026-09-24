--------------------------------------------------------------------------------------------------------------------------
-- 20260924-01-pipelines-sfft.sql
--
-- `pipelines` row for the SFFT differencer so `[sfft] register_sfft` has a
-- ppid to register SFFT difference-imaging results under. Assigned by the
-- lead 2026-09-24: ppid 16, priority 6 (both free -- the baseline's live
-- rows are 12/priority 4, 15/priority 3, 17/priority 5). `pipelines.ppid`
-- and `.priority` are plain smallint columns with UNIQUE constraints, not
-- sequences (20260921-01-baseline.sql), and existing grants on
-- `ALL TABLES IN SCHEMA public` (20260923-01/-03) already cover this row --
-- nothing further to grant here.
--------------------------------------------------------------------------------------------------------------------------

INSERT INTO pipelines (ppid,priority,script,descrip) values (16,6,'sfft_rapid_rimtimsim.py','SFFT difference-imaging pipeline for input SCA image.');
