"""
Ingest NO2 readings for RIVM Luchtmeetnet station NL10240 (Breda-Tilburgseweg).

Fetches the most recent hourly readings from the open API, keeps only NO2, and
upserts them into the `sensor_readings` table. Without DB_HOST set it only prints
the latest reading, which is enough for the local Docker check in Lab 1.

CAP note
--------
The sensor network is an AP system. When a station loses connectivity the API still
answers (Available, Partition-tolerant) but the value for that hour can be missing,
null, or a stale value carried over from the last good measurement, so it gives up
Consistency. A production pipeline must therefore never trust a value just because the
request succeeded:
  * a null/missing value is stored as NULL and flagged (is_flagged = TRUE), not dropped,
    because a silent gap in the NO2 time series is worse than a visible flagged one;
  * a value that does not change for 3+ consecutive hours is treated as stale and flagged
    the same way (Day 2 data quality handler);
  * flagged rows are logged as structured warnings so they can be counted and alerted on.

Failure handling
----------------
Each run re-fetches the last ~50 hours and writes with ON CONFLICT DO NOTHING. If the API
is unreachable, the run logs an error and exits non-zero; the next hourly run fills the
gap automatically because the window overlaps. No retry loop is needed at this cadence.
"""

import json
import logging
import os
import sys

import pandas as pd
import requests

STATION = "NL10240"
FORMULA = "NO2"
API_URL = "https://api.luchtmeetnet.nl/open_api/stations/{station}/measurements"
TIMEOUT_S = 10

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("ingest_air")


def log_event(level: int, event: str, **fields) -> None:
    """Emit one structured JSON log line."""
    log.log(level, json.dumps({"event": event, "source": "Luchtmeetnet", **fields}, default=str))


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


def write_to_db(df: pd.DataFrame) -> int:
    """Upsert rows into sensor_readings. Returns the number of newly inserted rows."""
    import psycopg2
    from psycopg2.extras import execute_values

    rows = [
        (r.station_id, r.timestamp, r.component, None if pd.isna(r.value) else float(r.value))
        for r in df.itertuples(index=False)
    ]
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "airbreda"),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=10,
    )
    try:
        with conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO sensor_readings (station_id, timestamp, component, value)
                VALUES %s
                ON CONFLICT (station_id, timestamp, component) DO NOTHING
                """,
                rows,
            )
            return cur.rowcount
    finally:
        conn.close()


def main() -> int:
    try:
        measurements = fetch_measurements()
    except requests.RequestException as exc:
        log_event(logging.ERROR, "fetch_failed", station_id=STATION, error=str(exc))
        return 1

    df = filter_no2_readings(to_dataframe(measurements))
    if df.empty:
        log_event(logging.WARNING, "no_data", station_id=STATION)
        return 1

    latest = df.iloc[0]
    log_event(logging.INFO, "fetch_success", station_id=STATION,
              value=None if pd.isna(latest.value) else float(latest.value),
              timestamp=latest.timestamp, rows=len(df))

    if not os.environ.get("DB_HOST"):
        log_event(logging.INFO, "db_skipped", reason="DB_HOST not set")
        return 0

    try:
        inserted = write_to_db(df)
    except Exception as exc:  # surface DB errors explicitly instead of swallowing them
        log_event(logging.ERROR, "db_write_failed", error=str(exc))
        return 1
    log_event(logging.INFO, "db_write_success", rows_fetched=len(df), rows_inserted=inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
