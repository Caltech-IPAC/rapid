-- surveys: the calibration, core community (CCS) and general astrophysics (GAS) programs, titled from STScI's Roman program pages (Azalee, 2026-11-17); schema.sql carries them for a fresh database, this file adds them to an existing one where missing, as postgres; a program_id already present is left as it is, so it may be rerun.

BEGIN;

INSERT INTO surveys (program_id, program_title, category) VALUES
    (1064, 'WFI dark current calibration', 'CAL'),
    (1065, 'WFI photometric and astrometric performance', 'CAL'),
    (1066, 'WFI flat field and linearity calibration', 'CAL'),
    (1067, 'WFI Photometric Stability', 'CAL'),
    (1068, 'WFI Photometry Spatial Uniformity', 'CAL'),
    (1069, 'Flux Calibration', 'CAL'),
    (1070, 'CAL Bandpass Variation Characterization', 'CAL'),
    (1071, 'Roman WFI Sub-pixel Response Calibration', 'CAL'),
    (1072, 'Roman WFI Cross Calibration Program', 'CAL'),
    (1073, 'Ice Characterization (Imaging)', 'CAL'),
    (1074, 'Ice decontamination and rebaseline', 'CAL'),
    (1075, 'Count-rate dependent nonlinearity (Direct)', 'CAL'),
    (1076, 'Lamp-On/Lamp-Off (LOLO) Calibration', 'CAL'),
    (1077, 'Dispersion Solution - Grism', 'CAL'),
    (1078, 'Dispersion Solution - Prism', 'CAL'),
    (1079, 'Edge/Trace mapping, Spectral PSF, and Relative flux - Grism', 'CAL'),
    (1080, 'Edge/Trace mapping, Spectral PSF, and Relative flux - Prism', 'CAL'),
    (1081, 'Absolute flux and spectrophotometric stability - Grism', 'CAL'),
    (1082, 'Absolute flux and spectrophotometric stability - Prism', 'CAL'),
    (1400, 'Roman High-Latitude Wide-Area Survey', 'CCS'),
    (1410, 'Roman High Latitude Time Domain Survey', 'CCS'),
    (1420, 'Galactic Bulge Time Domain Survey', 'CCS'),
    (1430, 'Roman Galactic Plane Survey', 'GAS'),
    (2000, 'The First Complete Census of Stars in Key Globular Clusters', 'GAS'),
    (2001, 'Roman eXtreme Deep Field (RXDF): transformative science at Roman''s limit', 'GAS'),
    (2002, 'A Legacy Survey of Andromeda and Triangulum with Roman', 'GAS'),
    (2003, 'GRACE: The Grism Reionization And Cosmic Evolution Survey', 'GAS'),
    (2004, 'The Roman-Kepler Legacy Survey', 'GAS')
ON CONFLICT (program_id) DO NOTHING;

COMMIT;
