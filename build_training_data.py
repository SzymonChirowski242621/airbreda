"""
Build training_data.csv by joining stored NO2 readings with stored NDW traffic snapshots.

Nothing here is new data: NO2 comes from `sensor_readings` (written by ingest_air.py) and
traffic from the hourly CSVs in S3 (written by ingest_traffic.py).

Hour alignment
--------------
Luchtmeetnet labels each value with the END of its averaging hour: timestamp 12:00 is the
mean over 11:00-12:00. The traffic job runs at :00 and stores NDW's latest one-minute
snapshot, which is from a few minutes earlier (e.g. 11:55, stored as ndw/.../11-hrl.csv).
So traffic hour H is joined to the NO2 value whose averaging hour starts at H, i.e. the
value labelled H+1. Joining both on their own labels would pair 11:55 traffic with the
10:00-11:00 average, which is the hour before.

Flagged NO2 rows (null or stale) are excluded from training.

Run it where both stores are reachable, e.g. on the VM:
    docker run --rm --user "$(id -u):$(id -g)" --env-file .env -v "$PWD:/work" -w /work \
      -e PYTHONPATH=/app airbreda-air python build_training_data.py
Or locally from synced files:
    python build_training_data.py --no2-csv no2.csv --traffic-dir ndw/
"""

import argparse
import io
import os
from pathlib import Path

import pandas as pd

SITES = ["hrl", "hrr", "vwd", "vwa"]
STATION = "NL10240"


def load_no2_from_db() -> pd.DataFrame:
    import status

    with status.connect() as conn:
        return pd.read_sql(
            """
            SELECT timestamp, value AS no2_ug_m3, is_flagged
            FROM sensor_readings
            WHERE station_id = %s AND component = 'NO2'
            ORDER BY timestamp
            """,
            conn, params=(STATION,),
        )


def load_traffic_from_s3(bucket: str) -> pd.DataFrame:
    import boto3

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-north-1"))
    frames = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="ndw/"):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            frames.append(pd.read_csv(io.BytesIO(body)))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_traffic_from_dir(path: Path) -> pd.DataFrame:
    frames = [pd.read_csv(p) for p in sorted(path.rglob("*.csv"))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build(no2: pd.DataFrame, traffic: pd.DataFrame) -> pd.DataFrame:
    """One row per hour that has a clean NO2 value and a reading from all four sites."""
    no2 = no2.copy()
    no2["timestamp"] = pd.to_datetime(no2["timestamp"], utc=True)
    no2 = no2[~no2["is_flagged"].astype(bool) & no2["no2_ug_m3"].notna()]
    no2["hour_start"] = no2["timestamp"] - pd.Timedelta(hours=1)

    traffic = traffic.copy()
    traffic["hour_start"] = pd.to_datetime(traffic["timestamp"], utc=True).dt.floor("h")
    wide = traffic.pivot_table(index="hour_start", columns="site",
                               values="intensity_veh_per_hr", aggfunc="last")
    wide = wide.reindex(columns=SITES).dropna()  # an hour needs all four sites
    wide.columns = [f"intensity_{c}" for c in wide.columns]
    wide["total_intensity_veh_per_hr"] = wide[[f"intensity_{s}" for s in SITES]].sum(axis=1)
    wide = wide.reset_index()

    out = no2[["hour_start", "no2_ug_m3"]].merge(wide, on="hour_start", how="inner")
    out["hour_of_day"] = out["hour_start"].dt.hour
    cols = ["hour_start", "no2_ug_m3", "total_intensity_veh_per_hr", "hour_of_day"] + \
           [f"intensity_{s}" for s in SITES]
    return out[cols].sort_values("hour_start").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no2-csv", type=Path, help="read NO2 from a CSV instead of the database")
    ap.add_argument("--traffic-dir", type=Path, help="read traffic CSVs from a folder instead of S3")
    ap.add_argument("--out", type=Path, default=Path("training_data.csv"))
    args = ap.parse_args()

    no2 = pd.read_csv(args.no2_csv) if args.no2_csv else load_no2_from_db()
    traffic = load_traffic_from_dir(args.traffic_dir) if args.traffic_dir \
        else load_traffic_from_s3(os.environ["S3_BUCKET"])

    df = build(no2, traffic)
    df.to_csv(args.out, index=False)
    print(f"NO2 rows: {len(no2)}, traffic rows: {len(traffic)}, joined rows: {len(df)} -> {args.out}")
    if len(df):
        print(f"from {df['hour_start'].min()} to {df['hour_start'].max()}")


if __name__ == "__main__":
    main()
