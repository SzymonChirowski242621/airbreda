"""
Shared database helpers for ingestion status and data quality events.

The ingestion containers are started by cron with `docker run --rm` and exit after one run,
so an in-memory counter or a per-container /health endpoint would be gone a minute later.
Instead each run records its outcome in two small tables (see schema.sql):

  ingestion_status     one row per source: last successful fetch, last error, last alert
  data_quality_events  one row per bad value (NDW speed=-1, stale/null NO2)

The dashboard's GET /health (Day 4) reads these tables, so health reflects every run,
not just the process that happens to be alive.
"""

import os
from datetime import datetime, timedelta, timezone

BAD_DATA_THRESHOLD = 10          # more than this many bad values ...
BAD_DATA_WINDOW = timedelta(hours=1)  # ... within this window logs one ERROR


def connect():
    import psycopg2

    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "airbreda"),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=10,
    )


def record_success(conn, source: str, measurement_ts) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_status (source, last_successful_fetch, last_measurement_timestamp)
            VALUES (%s, now(), %s)
            ON CONFLICT (source) DO UPDATE
              SET last_successful_fetch = EXCLUDED.last_successful_fetch,
                  last_measurement_timestamp = EXCLUDED.last_measurement_timestamp
            """,
            (source, measurement_ts),
        )


def record_error(conn, source: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_status (source, last_error, last_error_at)
            VALUES (%s, %s, now())
            ON CONFLICT (source) DO UPDATE
              SET last_error = EXCLUDED.last_error, last_error_at = EXCLUDED.last_error_at
            """,
            (source, error[:500]),
        )


def record_bad_data(conn, source: str, events: list[dict]) -> None:
    """events: dicts with location, field, value, reason, timestamp."""
    if not events:
        return
    from psycopg2.extras import execute_values

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO data_quality_events
              (source, location, field, value, reason, measurement_timestamp)
            VALUES %s
            """,
            [(source, e.get("location"), e.get("field"), e.get("value"), e.get("reason"),
              e.get("timestamp")) for e in events],
        )


def bad_data_count(conn, source: str, window: timedelta = BAD_DATA_WINDOW) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM data_quality_events WHERE source = %s AND recorded_at > now() - %s",
            (source, window),
        )
        return cur.fetchone()[0]


def should_alert(count: int, last_alerted_at, now: datetime | None = None) -> bool:
    """Alert once per window: only when over threshold and not already alerted in the window."""
    now = now or datetime.now(timezone.utc)
    if count <= BAD_DATA_THRESHOLD:
        return False
    return last_alerted_at is None or now - last_alerted_at > BAD_DATA_WINDOW


def check_threshold(conn, source: str) -> int | None:
    """Return the bad-data count if an alert should be logged now (and mark it), else None."""
    count = bad_data_count(conn, source)
    with conn.cursor() as cur:
        cur.execute("SELECT threshold_alerted_at FROM ingestion_status WHERE source = %s", (source,))
        row = cur.fetchone()
        if not should_alert(count, row[0] if row else None):
            return None
        cur.execute(
            """
            INSERT INTO ingestion_status (source, threshold_alerted_at) VALUES (%s, now())
            ON CONFLICT (source) DO UPDATE SET threshold_alerted_at = now()
            """,
            (source,),
        )
    return count


def health(conn) -> dict:
    """Shape of the aggregated GET /health response (Day 5 contract)."""
    out = {"status": "ok"}
    with conn.cursor() as cur:
        for source, key in (("Luchtmeetnet", "luchtmeetnet"), ("NDW", "ndw")):
            cur.execute("SELECT last_successful_fetch FROM ingestion_status WHERE source = %s",
                        (source,))
            row = cur.fetchone()
            last = row[0] if row else None
            out[key] = {
                "last_successful_fetch": last.isoformat() if last else None,
                "bad_data_count": bad_data_count(conn, source),
            }
            if last is None or datetime.now(timezone.utc) - last > timedelta(hours=2):
                out["status"] = "degraded"
    return out


if __name__ == "__main__":
    import json

    with connect() as c:
        print(json.dumps(health(c), indent=2))
