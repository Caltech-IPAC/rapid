-- surveys: one negative id per simulation and the commissioning programs (Azalee, 2026-11-05 and 2026-11-10, rapid_docs decisions.md); schema.sql carries them for a fresh database, this file brings an existing one to it, applied once, in one transaction, as postgres.

BEGIN;

-- -1 is the GBTDS simulation's id alone.
UPDATE surveys SET program_title = 'SOCSIMS GBTDS' WHERE program_id = -1;

-- -4 for the HLWAS simulation and the twelve commissioning programs, where missing.
INSERT INTO surveys (program_id, program_title, category) VALUES
    (-4, 'SOCSIMS HLWAS', 'SIM'),
    (1006, 'WFI photometric and astrometric performance', 'COM'),
    (1007, 'WFI Bandpass Uniformity', 'COM'),
    (1008, 'WFI Photometric Stability', 'COM'),
    (1018, 'Crowded Field Time Series', 'COM'),
    (1020, 'Backgrounds', 'COM'),
    (1043, 'Ice Characterization (Imaging)', 'COM'),
    (1049, 'RAPID Implementation', 'COM'),
    (1053, 'High-Extinction Guiding Opportunity Activity', 'COM'),
    (1054, 'Early GBTDS observations', 'COM'),
    (1055, 'Time Series of a Second Crowded Field', 'COM'),
    (1062, 'Early Demonstration of HLTDS observation strategy and performance', 'COM'),
    (1083, 'Ecliptic Plane Moving Object Study', 'COM')
ON CONFLICT (program_id) DO NOTHING;

-- An earlier title read "Guiging".
UPDATE surveys SET program_title = 'High-Extinction Guiding Opportunity Activity' WHERE program_id = 1053;

-- The HLWAS simulation's exposures written under -1, by its datasets' names (r00342-local, r00342-inj1, ...).
UPDATE exposures SET program_id = -4 WHERE program_id = -1 AND dataset LIKE 'r00342%';

COMMIT;
