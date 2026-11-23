import pandas as pd

from ingest_air import filter_no2_readings, to_dataframe


def test_filter_no2_readings_handles_null_value():
    df = pd.DataFrame({
        "component": ["NO2", "NO2", "PM10"],
        "value": [18.4, None, 22.1],
        "timestamp": ["2024-01-15T08:00:00Z", "2024-01-15T09:00:00Z", "2024-01-15T08:00:00Z"],
    })
    result = filter_no2_readings(df)
    assert len(result) == 2
    assert result["value"].isnull().sum() == 1  # null NO2 rows are kept, not silently dropped


def test_to_dataframe_maps_api_records_to_table_shape():
    records = [
        {"formula": "NO2", "value": 37.59, "timestamp_measured": "2026-11-23T10:00:00+00:00"},
        {"formula": "NO2", "value": None, "timestamp_measured": "2026-11-23T09:00:00+00:00"},
    ]
    df = to_dataframe(records)
    assert list(df.columns) == ["station_id", "timestamp", "component", "value"]
    assert (df["station_id"] == "NL10240").all()
    assert df["value"].isnull().sum() == 1
