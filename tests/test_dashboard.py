from fastapi.testclient import TestClient

import dashboard

NO2 = {"timestamp": "2026-11-25T09:00:00+00:00", "value": 31.2}
TRAFFIC = {s: {"intensity": v, "timestamp": "2026-11-25T08:55:00Z"}
           for s, v in {"hrl": 1800.0, "hrr": 1500.0, "vwd": 300.0, "vwa": 400.0}.items()}


def client(monkeypatch, traffic=TRAFFIC):
    dashboard._cache.clear()
    monkeypatch.setattr(dashboard, "latest_no2", lambda: NO2)
    monkeypatch.setattr(dashboard, "latest_traffic", lambda: traffic)
    return TestClient(dashboard.app)


def test_site_returns_contract_fields(monkeypatch):
    body = client(monkeypatch).get("/site/hrl").json()
    assert body["site_id"] == "hrl"
    assert body["no2_ug_m3"] == 31.2
    assert body["intensity_veh_per_hr"] == 1800.0
    assert 0 <= body["no2_exceedance_risk"] <= 1
    assert body["timestamp"] == NO2["timestamp"]


def test_all_sites_share_one_prediction_from_total_traffic(monkeypatch):
    c = client(monkeypatch)
    preds = {c.get(f"/site/{s}").json()["no2_ug_m3_predicted"] for s in dashboard.SITES}
    assert len(preds) == 1


def test_prediction_failure_degrades_to_real_values(monkeypatch):
    def boom(*_):
        raise RuntimeError("model missing")
    c = client(monkeypatch)
    monkeypatch.setattr(dashboard.predictor, "predict", boom)
    r = c.get("/site/vwa")
    assert r.status_code == 200
    body = r.json()
    assert body["no2_ug_m3"] == 31.2 and body["intensity_veh_per_hr"] == 400.0
    assert body["no2_exceedance_risk"] is None and "model missing" in body["prediction_error"]


def test_unknown_site_is_404(monkeypatch):
    assert client(monkeypatch).get("/site/xyz").status_code == 404


def test_index_page_calls_the_api(monkeypatch):
    html = client(monkeypatch).get("/").text
    assert 'fetch("/site/" + s)' in html
