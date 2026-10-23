-- migrate: no-transaction, requires-extension timescaledb
-- 0002_timescale: hypertable and compression for samples.
-- Applied by hub.storage.migrate only when the timescaledb extension is available (or
-- HUB_DB_TIMESCALE=on). Retention and the hourly roll-up are done by the hub's maintenance job
-- (hub/maintenance.py) in both modes, so plain PostgreSQL and TimescaleDB behave the same;
-- with TimescaleDB the job drops whole chunks instead of deleting rows.

CREATE EXTENSION IF NOT EXISTS timescaledb;

SELECT create_hypertable('samples', 'ts', chunk_time_interval => INTERVAL '7 days',
                         migrate_data => true, if_not_exists => true);

ALTER TABLE samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'tag_id',
                         timescaledb.compress_orderby = 'ts DESC');

SELECT add_compression_policy('samples', INTERVAL '14 days', if_not_exists => true);
