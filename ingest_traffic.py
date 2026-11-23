"""
Ingest live NDW traffic readings for the four A27/Breda sites next to NO2 station NL10240.

Downloads the NDW site-config and measured-values feeds once per run, extracts each site
with the course's reference parser, and saves one CSV per site per hour:

    ndw/YYYY-MM-DD/HH-{site}.csv    (date/hour taken from the measurement, in UTC)

Each file is uploaded to S3 when S3_BUCKET is set (credentials come from the EC2 instance
role, never from a copied key).

Why both a database and a bucket
--------------------------------
The bucket (object storage) is the cheap, durable, append-only record of what NDW actually
sent: one immutable file per site per hour. It is the audit trail and, above all, the ML
training set. NDW only publishes live data, so when the model is retrained six months from
now these files are the *only* place the historical traffic exists. Object storage also lets
us re-parse everything later if the parsing logic changes, without touching a live table.

The database gives what the bucket cannot: indexed, low-latency queries such as "latest NO2
for NL10240" or "all readings between x and y", constraints that reject duplicates
(PRIMARY KEY + ON CONFLICT DO NOTHING), and joins between air quality and traffic. Scanning
thousands of small files for a dashboard request would be slow; storing raw XML in Postgres
would be expensive and pointless. Each store does the job it is good at.
"""

import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from reference.getTrafficReadings import (
    CONFIG_URL,
    MEASURED_URL,
    build_index_map,
    download_and_decompress,
    extract_measurements,
)

SITES = {
    "hrl": "RWS01_MONIBAS_0271hrl0063ra",  # A27 mainline, direction 1
    "hrr": "RWS01_MONIBAS_0271hrr0063ra",  # A27 mainline, direction 2
    "vwd": "RWS01_MONIBAS_0270vwd0063ra",  # entry slip road (leaving Breda)
    "vwa": "RWS01_MONIBAS_0270vwa0063ra",  # exit slip road (entering Breda)
}
CSV_FIELDS = ["site", "site_id", "timestamp", "intensity_veh_per_hr", "avg_speed_kmh",
              "lanes", "bad_value_count"]

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("ingest_traffic")


def log_event(level: int, event: str, **fields) -> None:
    """Emit one structured JSON log line."""
    log.log(level, json.dumps({"event": event, "source": "NDW", **fields}, default=str))


def summarise_site(label: str, site_id: str, index_map: dict, readings: list[dict]) -> dict:
    """Collapse per-lane readings into one row: total intensity and average speed.

    NDW uses -1 as a sentinel for "no valid measurement". Those values are excluded from
    the totals, logged as DATA_QUALITY_ERROR and counted, instead of being silently dropped
    (the reference script's `> 0` filter hides them).
    """
    flow_idx = {i for i, m in index_map.items()
                if m["type"] == "trafficFlow" and m["vehicle"] == "anyVehicle"}
    speed_idx = {i for i, m in index_map.items()
                 if m["type"] == "trafficSpeed" and m["vehicle"] == "anyVehicle"}

    flows, speeds, bad = [], [], 0
    for r in readings:
        field = "flow" if r["index"] in flow_idx else "speed" if r["index"] in speed_idx else None
        if field is None:
            continue
        value = float(r["value"])
        if value < 0:
            bad += 1
            log_event(logging.WARNING, "DATA_QUALITY_ERROR", location=site_id, site=label,
                      field=field, value=value, timestamp=r.get("timestamp"))
            continue
        (flows if field == "flow" else speeds).append(value)

    moving = [s for s in speeds if s > 0]  # 0 km/h = no vehicles that minute, not an error
    return {
        "site": label,
        "site_id": site_id,
        "timestamp": readings[0].get("timestamp") if readings else None,
        "intensity_veh_per_hr": sum(flows) if flows else None,
        "avg_speed_kmh": round(sum(moving) / len(moving), 1) if moving else None,
        "lanes": len(flow_idx),
        "bad_value_count": bad,
    }


def object_key(row: dict) -> str:
    """ndw/YYYY-MM-DD/HH-{site}.csv, based on the measurement time (UTC), not on 'now'."""
    ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return f"ndw/{ts:%Y-%m-%d}/{ts:%H}-{row['site']}.csv"


def write_csv(row: dict, out_dir: Path, key: str) -> Path:
    path = out_dir / Path(key).relative_to("ndw")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerow(row)
    return path


def upload(path: Path, bucket: str, key: str) -> None:
    import boto3

    boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-north-1")).upload_file(
        str(path), bucket, key)


def main() -> int:
    out_dir = Path(os.environ.get("OUTPUT_DIR", "ndw"))
    bucket = os.environ.get("S3_BUCKET")

    try:
        config_bytes = download_and_decompress(CONFIG_URL).read()
        measured_bytes = download_and_decompress(MEASURED_URL).read()
    except Exception as exc:
        log_event(logging.ERROR, "fetch_failed", error=str(exc))
        return 1

    saved = 0
    for label, site_id in SITES.items():
        index_map = build_index_map(io.BytesIO(config_bytes), site_id)
        readings = extract_measurements(io.BytesIO(measured_bytes), site_id)
        row = summarise_site(label, site_id, index_map, readings)
        if row["timestamp"] is None:
            log_event(logging.WARNING, "no_data", location=site_id, site=label)
            continue

        key = object_key(row)
        path = write_csv(row, out_dir, key)
        if bucket:
            try:
                upload(path, bucket, key)
            except Exception as exc:
                log_event(logging.ERROR, "upload_failed", site=label, key=key, error=str(exc))
                continue
        log_event(logging.INFO, "fetch_success", location=site_id, site=label, key=key,
                  uploaded=bool(bucket), intensity_veh_per_hr=row["intensity_veh_per_hr"],
                  avg_speed_kmh=row["avg_speed_kmh"], timestamp=row["timestamp"])
        saved += 1

    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
