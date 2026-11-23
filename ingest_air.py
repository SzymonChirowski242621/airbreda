"""
Ingest NO2 readings for RIVM Luchtmeetnet station NL10240 (Breda-Tilburgseweg).

Fetches the most recent hourly readings from the open API, keeps only NO2, flags stale or
null values, and upserts them into `sensor_readings`. Without DB_HOST set it only logs,
which is enough for a local Docker check.

CAP note
--------
The sensor network is an AP system. When a station loses connectivity the API still
answers (Available, Partition-tolerant) but the value for that hour can be missing,
null, or a stale value carried over from the last good measurement, so it gives up
Consistency. A production pipeline must therefore never trust a value just because the
request succeeded:
  * a null/missing value is stored as NULL and flagged (is_flagged = TRUE), not dropped,
    because a silent gap in the NO2 time series is worse than a visible flagged one;
  * a value that is identical for 3+ consecutive hours is treated as stale and flagged
    the same way;
  * every newly flagged row is logged as a structured DATA_QUALITY_ERROR warning and
    recorded in data_quality_events, so it can be counted and alerted on.

Failure handling
----------------
Each run re-fetches the last ~50 hours. If the API is unreachable, the run logs an error,
records it in ingestion_status and exits non-zero; the next hourly run fills the gap
automatically because the window overlaps. No retry loop is needed at this cadence.
Writes are idempotent: an existing row keeps its value, and only its is_flagged can change
(a value can become "stale" once later hours arrive).
"""

import json
import logging
import os
import sys
from datetime import timedelta

import pandas as pd
import requests

import status

SOURCE = "Luchtmeetnet"
STATION = "NL10240"
FORMULA = "NO2"
API_URL = "https://api.luchtmeetnet.nl/open_api/stations/{station}/measurements"
TIMEOUT_S = 10
STALE_RUN = 3  # this many identical consecutive hourly values = stale

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("ingest_air")

luchtmeetnet_bad_data_count = 0  # bad values seen in this run (persisted in data_quality_events)


def log_event(level: int, event: str, **fields) -> None:
    """Emit one structured JSON log line."""
    log.log(level, json.dumps({"event": event, "source": SOURCE, **fields}, default=str))


def fetch_measurements(station: str = STATION, formula: str = FORMULA) -> list[dict]:
    """Return the most recent page (up to 50 hourly rows) of measurements, newest first.

    Builds on get_latest_no2() from the course's getNO2Readings.py, but keeps the whole
    page instead of only the first row so each run also repairs recent gaps.
    """
    params = {
        "formula": formula,
        "order_by": "timestamp_measured",
        "order_direction": "desc",
        "page": 1,
    }
    response = requests.get(API_URL.format(station=station), params=params, timeout=TIMEOUT_S)
    response.raise_for_status()
    return response.json().get("data", [])


def to_dataframe(measurements: list[dict], station: str = STATION) -> pd.DataFrame:
    """Normalise API records to the sensor_readings shape."""
    df = pd.DataFrame(measurements, columns=["formula", "value", "timestamp_measured"])
    return pd.DataFrame({
        "station_id": station,
        "timestamp": df["timestamp_measured"],
        "component": df["formula"],
        "value": pd.to_numeric(df["value"], errors="coerce"),
    })


def filter_no2_readings(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only NO2 rows. Rows with a null value are kept on purpose (see CAP note)."""
    return df[df["component"] == "NO2"].reset_index(drop=True)


def flag_readings(df: pd.DataFrame) -> pd.DataFrame:
    """Add is_flagged and flag_reason; returns rows sorted oldest first.

    null:  value is missing.
    stale: value equals the two previous hourly values (3 identical in a row). The first two
           values of a run are left unflagged: two equal hours in a row happen naturally.
           A gap in the hourly timestamps breaks the run.
    """
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.sort_values("timestamp").reset_index(drop=True)

    same_as_prev = (out["value"] == out["value"].shift(1)) & \
                   (out["timestamp"] - out["timestamp"].shift(1) == timedelta(hours=1))
    run = same_as_prev.astype(int).groupby((~same_as_prev).cumsum()).cumsum() + 1
    stale = out["value"].notna() & (run >= STALE_RUN)
    null = out["value"].isna()

    out["is_flagged"] = null | stale
    out["flag_reason"] = None
    out.loc[stale, "flag_reason"] = "stale"
    out.loc[null, "flag_reason"] = "null"
    return out


def rows_to_write(df: pd.DataFrame, existing: dict) -> pd.DataFrame:
    """Rows that are new, or whose flag changed. existing: {timestamp: is_flagged}.

    Without this, every hourly run would re-log and re-count the same bad rows from its
    50-hour window, and bad_data_count would be inflated ~50x.
    """
    keep = [ts not in existing or existing[ts] != flagged
            for ts, flagged in zip(df["timestamp"], df["is_flagged"])]
    return df[keep].reset_index(drop=True)


def build_rows(df: pd.DataFrame) -> list[tuple]:
    """Tuples for INSERT, in sensor_readings column order."""
    return [
        (r.station_id, r.timestamp.isoformat(), r.component,
         None if pd.isna(r.value) else float(r.value), bool(r.is_flagged))
        for r in df.itertuples(index=False)
    ]


def fetch_existing(conn, df: pd.DataFrame) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, is_flagged FROM sensor_readings
            WHERE station_id = %s AND component = %s AND timestamp >= %s
            """,
            (STATION, FORMULA, df["timestamp"].min().isoformat()),
        )
        return {pd.Timestamp(ts).tz_convert("UTC"): flagged for ts, flagged in cur.fetchall()}


def write_to_db(conn, df: pd.DataFrame) -> int:
    from psycopg2.extras import execute_values

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO sensor_readings (station_id, timestamp, component, value, is_flagged)
            VALUES %s
            ON CONFLICT (station_id, timestamp, component)
            DO UPDATE SET is_flagged = EXCLUDED.is_flagged
            """,
            build_rows(df),
        )
        return cur.rowcount


def log_flagged(df: pd.DataFrame) -> list[dict]:
    """Log one DATA_QUALITY_ERROR per flagged row and return them as events."""
    global luchtmeetnet_bad_data_count
    events = []
    for r in df[df["is_flagged"]].itertuples(index=False):
        value = None if pd.isna(r.value) else float(r.value)
        log_event(logging.WARNING, "DATA_QUALITY_ERROR", station_id=STATION, field=FORMULA,
                  reason=r.flag_reason, value=value, timestamp=r.timestamp.isoformat())
        luchtmeetnet_bad_data_count += 1
        events.append({"location": STATION, "field": FORMULA, "value": value,
                       "reason": r.flag_reason, "timestamp": r.timestamp.isoformat()})
    return events


def main() -> int:
    use_db = bool(os.environ.get("DB_HOST"))

    try:
        measurements = fetch_measurements()
    except requests.RequestException as exc:
        log_event(logging.ERROR, "fetch_failed", station_id=STATION, error=str(exc))
        if use_db:
            try:
                with status.connect() as conn:
                    status.record_error(conn, SOURCE, str(exc))
            except Exception as db_exc:
                log_event(logging.ERROR, "status_write_failed", error=str(db_exc))
        return 1

    df = flag_readings(filter_no2_readings(to_dataframe(measurements)))
    if df.empty:
        log_event(logging.WARNING, "no_data", station_id=STATION)
        return 1

    latest = df.iloc[-1]
    log_event(logging.INFO, "fetch_success", station_id=STATION,
              value=None if pd.isna(latest.value) else float(latest.value),
              timestamp=latest.timestamp.isoformat(), rows=len(df))

    if not use_db:
        log_flagged(df)
        log_event(logging.INFO, "db_skipped", reason="DB_HOST not set")
        return 0

    try:
        conn = status.connect()
        with conn:
            changed = rows_to_write(df, fetch_existing(conn, df))
            events = log_flagged(changed)
            written = write_to_db(conn, changed) if not changed.empty else 0
            status.record_bad_data(conn, SOURCE, events)
            status.record_success(conn, SOURCE, latest.timestamp.isoformat())
            alert_count = status.check_threshold(conn, SOURCE)
        conn.close()
    except Exception as exc:  # surface DB errors explicitly instead of swallowing them
        log_event(logging.ERROR, "db_write_failed", error=str(exc))
        return 1

    log_event(logging.INFO, "db_write_success", rows_fetched=len(df), rows_written=written,
              bad_data_count=luchtmeetnet_bad_data_count)
    if alert_count is not None:
        log_event(logging.ERROR, "BAD_DATA_THRESHOLD_EXCEEDED", count=alert_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
