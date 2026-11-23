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
