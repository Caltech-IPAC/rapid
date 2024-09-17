--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSWVersionsInserts
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 17 September 2024
--------------------------------------------------------------------------------------------------------------------------

-- Store first 7 characters of git hash in cvstag field.

INSERT INTO swversions (cvstag,installed,comment,release) values ('23c83dd',now(),'Development', '0.1');
