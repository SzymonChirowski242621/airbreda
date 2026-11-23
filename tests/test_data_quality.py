from datetime import datetime, timedelta, timezone

import pandas as pd

import ingest_traffic
from ingest_air import build_rows, flag_readings, rows_to_write
from status import should_alert


def _no2(values, start="2026-11-23T00:00:00Z"):
    t0 = pd.Timestamp(start)
    return pd.DataFrame({
        "station_id": "NL10240",
        "timestamp": [(t0 + timedelta(hours=i)).isoformat() for i in range(len(values))],
        "component": "NO2",
        "value": values,
    })


def test_null_luchtmeetnet_reading_is_written_flagged_not_dropped():
    df = flag_readings(_no2([18.4, None, 20.1]))
    rows = build_rows(rows_to_write(df, existing={}))
    assert len(rows) == 3                     # nothing dropped
    null_row = [r for r in rows if r[3] is None]
    assert null_row and null_row[0][4] is True  # value NULL, is_flagged TRUE


def test_stale_luchtmeetnet_reading_flagged_from_third_identical_hour():
    df = flag_readings(_no2([18.4, 18.4, 18.4, 18.4, 21.0]))
    assert df["is_flagged"].tolist() == [False, False, True, True, False]
    assert set(df.loc[df["is_flagged"], "flag_reason"]) == {"stale"}


def test_gap_in_hours_breaks_a_stale_run():
    df = _no2([18.4, 18.4, 18.4])
    df.loc[2, "timestamp"] = "2026-11-23T05:00:00+00:00"  # 4 hours after the previous one
    assert not flag_readings(df)["is_flagged"].any()


def test_rows_already_stored_are_not_rewritten_or_recounted():
    df = flag_readings(_no2([18.4, None]))
    existing = dict(zip(df["timestamp"], df["is_flagged"]))
    assert rows_to_write(df, existing).empty


def test_ndw_speed_minus_one_is_not_stored_and_increments_counter():
    index_map = {"1": {"type": "trafficFlow", "vehicle": "anyVehicle"},
                 "2": {"type": "trafficSpeed", "vehicle": "anyVehicle"}}
    readings = [{"index": "1", "value": "300", "timestamp": "2026-11-23T10:00:00Z"},
                {"index": "2", "value": "-1", "timestamp": "2026-11-23T10:00:00Z"}]
    before = ingest_traffic.ndw_bad_data_count
    row, events = ingest_traffic.summarise_site("vwa", "SITE", index_map, readings)
    assert row["avg_speed_kmh"] is None          # -1 never reaches the stored record
    assert row["intensity_veh_per_hr"] == 300
    assert ingest_traffic.ndw_bad_data_count == before + 1
    assert events[0]["field"] == "speed" and events[0]["value"] == -1


def test_threshold_alerts_once_per_hour():
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    assert not should_alert(10, None, now)                       # 10 is not "more than 10"
    assert should_alert(11, None, now)
    assert not should_alert(11, now - timedelta(minutes=30), now)
    assert should_alert(11, now - timedelta(minutes=61), now)
