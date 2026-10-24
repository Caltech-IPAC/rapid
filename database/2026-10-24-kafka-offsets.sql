-- A packet set's acknowledgement coordinates (night-2026-10-24 task 5): the
-- partition and the first and last offsets its messages were acknowledged at,
-- written with kafka_published_at, which scripts/consume_packets.py seeks to.
-- schema.sql carries the columns for a fresh database; this file brings an
-- existing one to it, applied once.
ALTER TABLE jobs ADD COLUMN kafka_partition integer,
                 ADD COLUMN kafka_first_offset bigint,
                 ADD COLUMN kafka_last_offset bigint;
