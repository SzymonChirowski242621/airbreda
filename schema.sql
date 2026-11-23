-- sensor_readings: parsed Luchtmeetnet readings.
-- is_flagged is included from the start (Day 2 flags stale/null NO2 instead of dropping it).
CREATE TABLE IF NOT EXISTS sensor_readings (
    station_id  VARCHAR(20) NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    component   VARCHAR(10) NOT NULL,
    value       FLOAT,
    is_flagged  BOOLEAN     NOT NULL DEFAULT FALSE,
    PRIMARY KEY (station_id, timestamp, component)
);

-- One row per source. Written by every ingestion run, read by GET /health.
CREATE TABLE IF NOT EXISTS ingestion_status (
    source                      VARCHAR(20) PRIMARY KEY,
    last_successful_fetch       TIMESTAMPTZ,
    last_measurement_timestamp  TIMESTAMPTZ,
    last_error                  TEXT,
    last_error_at               TIMESTAMPTZ,
    threshold_alerted_at        TIMESTAMPTZ
);

-- One row per bad value (NDW speed/flow = -1, stale or null NO2).
CREATE TABLE IF NOT EXISTS data_quality_events (
    id                     BIGSERIAL PRIMARY KEY,
    source                 VARCHAR(20) NOT NULL,
    location               VARCHAR(40),
    field                  VARCHAR(20),
    value                  FLOAT,
    reason                 VARCHAR(40),
    measurement_timestamp  TIMESTAMPTZ,
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS data_quality_events_source_time
    ON data_quality_events (source, recorded_at);
