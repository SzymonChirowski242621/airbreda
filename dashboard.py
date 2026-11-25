"""
AirBreda dashboard: JSON API + one HTML page, served from the same FastAPI app.

Where each field of GET /site/{site_id} comes from
--------------------------------------------------
  site_id               the path parameter (hrl, hrr, vwd or vwa)
  no2_ug_m3             latest unflagged NO2 value for station NL10240 in RDS `sensor_readings`.
                        One station covers all four sites: they sit at the same interchange.
  timestamp             end of that NO2 averaging hour (Luchtmeetnet's label), ISO 8601
  intensity_veh_per_hr  that site's latest CSV in S3 (ndw/YYYY-MM-DD/HH-{site}.csv)
  traffic_timestamp     the NDW snapshot time inside that CSV
  no2_ug_m3_predicted   predict() in predict.py, from model.pkl baked into this image
  no2_exceedance_risk   predict(): sigmoid of the predicted value around 40 ug/m3

The prediction uses the TOTAL intensity of all four sites at the latest hour, because that
is the feature the model was trained on. Feeding one site's intensity into a model trained
on the four-site total would be training-serving skew. So all four sites get the same
prediction, which is correct: there is one NO2 station and one prediction for the
interchange; what differs per site is the traffic.

If predict() raises
-------------------
The request does not fail. The route still returns the real NO2 and intensity values, with
no2_ug_m3_predicted and no2_exceedance_risk set to null and a `prediction_error` field. Real
measurements are the most useful part of the response and do not depend on the model; a
500 would hide them because of a problem in the least reliable part. The error is logged.
"""

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import predict as predictor
import status

SITES = ("hrl", "hrr", "vwd", "vwa")
STATION = "NL10240"
CACHE_SECONDS = 60

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("dashboard")
for noisy in ("boto3", "botocore", "s3transfer", "urllib3", "uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

app = FastAPI(title="AirBreda", docs_url=None, redoc_url=None)
_cache: dict = {}


def log_event(level: int, event: str, **fields) -> None:
    log.log(level, json.dumps({"event": event, "source": "dashboard", **fields}, default=str))


def cached(key: str, loader):
    """Tiny time-based cache so four /site calls from one page load hit each store once."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = loader()
    _cache[key] = (time.monotonic(), value)
    return value


@contextmanager
def db():
    """psycopg2's `with conn` only commits; this also closes, so requests don't leak connections."""
    conn = status.connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def latest_no2() -> dict:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, value FROM sensor_readings
            WHERE station_id = %s AND component = 'NO2' AND NOT is_flagged AND value IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1
            """,
            (STATION,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError("no NO2 readings stored yet")
    return {"timestamp": row[0].astimezone(timezone.utc).isoformat(), "value": float(row[1])}


def latest_traffic() -> dict:
    """Latest CSV per site from S3: {site: {"intensity": float, "timestamp": str}}."""
    import csv
    import io

    import boto3

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-north-1"))
    bucket = os.environ["S3_BUCKET"]
    today = datetime.now(timezone.utc)
    keys = []
    for day in (today, today - timedelta(days=1)):
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"ndw/{day:%Y-%m-%d}/")
        keys += [o["Key"] for o in resp.get("Contents", [])]

    out = {}
    for site in SITES:
        site_keys = sorted(k for k in keys if k.endswith(f"-{site}.csv"))
        if not site_keys:
            continue
        body = s3.get_object(Bucket=bucket, Key=site_keys[-1])["Body"].read().decode()
        row = next(csv.DictReader(io.StringIO(body)))
        if row["intensity_veh_per_hr"]:
            out[site] = {"intensity": float(row["intensity_veh_per_hr"]), "timestamp": row["timestamp"]}
    return out


def site_payload(site_id: str, no2: dict, traffic: dict) -> dict:
    if site_id not in traffic:
        raise LookupError(f"no traffic reading stored for {site_id}")
    payload = {
        "site_id": site_id,
        "no2_ug_m3": no2["value"],
        "intensity_veh_per_hr": traffic[site_id]["intensity"],
        "no2_ug_m3_predicted": None,
        "no2_exceedance_risk": None,
        "timestamp": no2["timestamp"],
        "traffic_timestamp": traffic[site_id]["timestamp"],
    }
    try:
        if set(traffic) != set(SITES):
            raise ValueError("total intensity needs all four sites")
        total = sum(t["intensity"] for t in traffic.values())
        snapshot = datetime.fromisoformat(traffic[site_id]["timestamp"].replace("Z", "+00:00"))
        payload.update(predictor.predict(total, snapshot.astimezone(timezone.utc).hour))
    except Exception as exc:  # degrade: keep the real values, drop the prediction
        payload["prediction_error"] = str(exc)
        log_event(logging.ERROR, "prediction_failed", site_id=site_id, error=str(exc))
    return payload


@app.get("/site/{site_id}")
def get_site(site_id: str):
    if site_id not in SITES:
        raise HTTPException(status_code=404, detail=f"unknown site; use one of {', '.join(SITES)}")
    try:
        no2 = cached("no2", latest_no2)
        traffic = cached("traffic", latest_traffic)
        payload = site_payload(site_id, no2, traffic)
    except LookupError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log_event(logging.ERROR, "site_failed", site_id=site_id, error=str(exc))
        raise HTTPException(status_code=503, detail="data store unavailable")
    log_event(logging.INFO, "site_served", site_id=site_id, no2=payload["no2_ug_m3"],
              intensity=payload["intensity_veh_per_hr"], risk=payload["no2_exceedance_risk"])
    return payload


@app.get("/health")
def get_health():
    try:
        with db() as conn:
            body = status.health(conn)
    except Exception as exc:
        log_event(logging.ERROR, "health_failed", error=str(exc))
        return JSONResponse(status_code=503, content={"status": "down", "error": "database unavailable"})
    return body


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirBreda · A27 Breda</title>
<style>
:root{--bg:#f5f7f8;--card:#fff;--fg:#16212b;--muted:#5a6875;--line:#d9e0e5;--no2:#a2501f;--traffic:#1f6f78;--warn:#9a6a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f161c;--card:#16202a;--fg:#e4eaee;--muted:#93a2ae;--line:#2a3742;--no2:#e08a55;--traffic:#5bbac4;--warn:#e0b44a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:32px max(16px,env(safe-area-inset-left))}
main{max-width:900px;margin:0 auto;display:grid;gap:24px}h1{margin:0;font-size:1.8rem}p{margin:0;color:var(--muted)}
.top{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}@media(max-width:640px){.top{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;gap:4px}
.big{font-size:2.2rem;font-weight:650;font-variant-numeric:tabular-nums}.lbl{color:var(--muted);font-size:.85rem}
.no2{color:var(--no2)}.traffic{color:var(--traffic)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:10px 14px;border-bottom:1px solid var(--line);text-align:left;font-variant-numeric:tabular-nums}th{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
td.n,th.n{text-align:right}tr:last-child td{border-bottom:0}.wrap{overflow-x:auto}
#stale{color:var(--warn);font-weight:600}
</style></head><body><main>
<div><h1>NO&#8322; and traffic at the A27 interchange, Breda</h1>
<p>Station NL10240 (Breda-Tilburgseweg) and NDW sites hrl, hrr, vwd, vwa. Refreshes every 5 minutes.</p></div>
<div class="top">
 <div class="card"><span class="lbl">Measured NO&#8322;</span><span class="big no2" id="actual">&ndash;</span><span class="lbl" id="actual-ts"></span></div>
 <div class="card"><span class="lbl">Predicted NO&#8322; (traffic model)</span><span class="big no2" id="pred">&ndash;</span><span class="lbl" id="risk"></span></div>
 <div class="card"><span class="lbl">Total traffic, all four sites</span><span class="big traffic" id="total">&ndash;</span><span class="lbl" id="traffic-ts"></span></div>
</div>
<div class="wrap"><table><thead><tr><th>Site</th><th>Location</th><th class="n">Vehicles/hour</th><th class="n">Predicted NO&#8322;</th><th class="n">Exceedance risk</th></tr></thead>
<tbody id="rows"></tbody></table></div>
<p>Last updated: <span id="updated">&ndash;</span> <span id="stale"></span></p>
<p>Exceedance risk is the chance the hour is above 40 &micro;g/m&sup3;, the EU annual limit value, applied here to hourly values. JSON: <a href="/site/hrl">/site/hrl</a> &middot; <a href="/health">/health</a></p>
</main>
<script>
const SITES = [["hrl","A27 mainline, direction 1"],["hrr","A27 mainline, direction 2"],["vwd","Entry ramp, leaving Breda"],["vwa","Exit ramp, entering Breda"]];
const fmt = (v, d=1) => v == null ? "n/a" : Number(v).toLocaleString("en-GB", {maximumFractionDigits:d});
const time = s => new Date(s).toLocaleString("en-GB", {day:"numeric", month:"short", hour:"2-digit", minute:"2-digit"});
async function load() {
  const res = await Promise.all(SITES.map(([s]) => fetch("/site/" + s).then(r => r.ok ? r.json() : null).catch(() => null)));
  const ok = res.filter(Boolean);
  const rows = document.getElementById("rows"); rows.innerHTML = "";
  SITES.forEach(([s, name], i) => {
    const r = res[i]; const tr = document.createElement("tr");
    tr.innerHTML = `<td><b>${s}</b></td><td>${name}</td><td class="n">${r ? fmt(r.intensity_veh_per_hr,0) : "n/a"}</td>` +
      `<td class="n">${r ? fmt(r.no2_ug_m3_predicted) + " &micro;g/m&sup3;" : "n/a"}</td><td class="n">${r && r.no2_exceedance_risk != null ? Math.round(r.no2_exceedance_risk*100) + "%" : "n/a"}</td>`;
    rows.appendChild(tr);
  });
  if (!ok.length) { document.getElementById("stale").textContent = "No data available right now."; return; }
  const a = ok[0];
  document.getElementById("actual").textContent = fmt(a.no2_ug_m3) + " µg/m³";
  document.getElementById("actual-ts").textContent = "hour ending " + time(a.timestamp);
  document.getElementById("pred").textContent = a.no2_ug_m3_predicted == null ? "n/a" : fmt(a.no2_ug_m3_predicted) + " µg/m³";
  document.getElementById("risk").textContent = a.no2_exceedance_risk == null ? (a.prediction_error || "") : "exceedance risk " + Math.round(a.no2_exceedance_risk*100) + "%";
  document.getElementById("total").textContent = fmt(ok.reduce((t, r) => t + r.intensity_veh_per_hr, 0), 0) + " veh/h";
  document.getElementById("traffic-ts").textContent = "NDW snapshot " + time(a.traffic_timestamp);
  const newest = Math.max(...ok.map(r => Date.parse(r.traffic_timestamp)), Date.parse(a.timestamp));
  document.getElementById("updated").textContent = time(new Date(newest).toISOString());
  document.getElementById("stale").textContent = Date.now() - newest > 2*3600*1000 ? "Data is more than 2 hours old: ingestion may have stopped." : "";
}
load(); setInterval(load, 5*60*1000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
