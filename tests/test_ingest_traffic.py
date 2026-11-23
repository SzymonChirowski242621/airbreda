from ingest_traffic import object_key, summarise_site

INDEX_MAP = {
    "1": {"type": "trafficFlow", "vehicle": "anyVehicle"},
    "2": {"type": "trafficFlow", "vehicle": "anyVehicle"},
    "3": {"type": "trafficSpeed", "vehicle": "anyVehicle"},
    "4": {"type": "trafficSpeed", "vehicle": "anyVehicle"},
    "5": {"type": "trafficFlow", "vehicle": "specific-class"},
}
TS = "2026-11-23T10:30:00Z"


def test_summarise_site_excludes_and_counts_negative_speed():
    readings = [
        {"index": "1", "value": "480", "timestamp": TS},
        {"index": "2", "value": "960", "timestamp": TS},
        {"index": "3", "value": "98", "timestamp": TS},
        {"index": "4", "value": "-1", "timestamp": TS},
        {"index": "5", "value": "60", "timestamp": TS},  # per-class flow, not a total
    ]
    row, _ = summarise_site("hrr", "SITE", INDEX_MAP, readings)
    assert row["intensity_veh_per_hr"] == 1440
    assert row["avg_speed_kmh"] == 98
    assert row["bad_value_count"] == 1
    assert row["lanes"] == 2


def test_object_key_uses_measurement_time_not_now():
    assert object_key({"timestamp": TS, "site": "hrl"}) == "ndw/2026-11-23/10-hrl.csv"
