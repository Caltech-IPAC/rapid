-----------------------------
-- DATABASE Configuration: ASSUME HERE the name of the database is rapidopsdb.
--
-- Timestamps stored in database records are tied to actions started from California,
-- such as processing start and end times in the Jobs database table.
-----------------------------

ALTER DATABASE rapidopsdb SET timezone TO 'America/Los_Angeles';

