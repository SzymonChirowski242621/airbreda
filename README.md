# AirBreda

A cloud data pipeline for the BUas *System Design & Cloud Platforms* elective (2026). It checks whether traffic at the A27 interchange near Breda goes together with higher NO₂ nearby.

Every hour it collects two kinds of live Dutch open data. NO₂ readings come from RIVM Luchtmeetnet (station NL10240, Breda-Tilburgseweg). Vehicle counts come from NDW, at four measurement sites on the A27. The data is stored on AWS in eu-north-1. A dashboard and JSON API serve the readings along with a NO₂ prediction from a regression model trained on the data this pipeline collected.

| | |
|---|---|
| Live dashboard | <http://16.192.171.1:8000/> |
| Architecture design document | <https://szymonchirowski242621.github.io/airbreda/> (source in `docs/`) |
| Repository | <https://github.com/SzymonChirowski242621/airbreda> |

## How it runs

One EC2 VM (`t3.small`) runs three containers:

| Container | Image / Dockerfile | Schedule | What it does |
|---|---|---|---|
| `airbreda-traffic` | `Dockerfile.traffic` | cron, `:00` every hour | Downloads the NDW feed and extracts intensity and speed for sites `hrl`, `hrr`, `vwd` and `vwa`. Uploads one CSV per site to S3 at `ndw/YYYY-MM-DD/HH-{site}.csv`. Values with `speed=-1` are logged and left out. |
| `airbreda-air` | `Dockerfile` | cron, `:15` every hour | Fetches the last 50 hours of NO₂ and upserts them into RDS PostgreSQL. Stale or null readings are kept and written with `is_flagged = TRUE`. |
| `airbreda-dashboard` | `Dockerfile.dashboard` | always on, port 8000 | FastAPI app with the dashboard page and JSON API. `model.pkl` is baked into the image. |

The ingestion containers exit after each run, so each run records its status and its bad-data events in the database (`ingestion_status`, `data_quality_events`). `/health` reads them from there. All three containers log structured JSON to stdout.

S3 access comes from an IAM instance role, so no AWS keys are stored on the VM. The database accepts connections only from the VM's security group.

## API

| Endpoint | Returns |
|---|---|
| `GET /` | Dashboard showing actual NO₂, predicted NO₂ and exceedance risk per site, and total traffic. Refreshes every 5 minutes. |
| `GET /site/{hrl,hrr,vwd,vwa}` | `site_id`, `no2_ug_m3`, `intensity_veh_per_hr`, `no2_ug_m3_predicted`, `no2_exceedance_risk` (0–1), `timestamp` |
| `GET /health` | `status`, and `last_successful_fetch` and `bad_data_count` for both `luchtmeetnet` and `ndw` |

If the model fails, `/site/{id}` still returns the real measurements. The prediction fields are then `null` (see ADR-006).

## Repository layout

| Path | Contents |
|---|---|
| `ingest_air.py`, `ingest_traffic.py` | Ingestion jobs, including both bad-data handlers |
| `dashboard.py`, `predict.py` | FastAPI app and model wrapper (prediction and sigmoid exceedance risk) |
| `status.py` | Shared database helpers for ingestion status and data-quality events |
| `broker.py`, `docker-compose.yml` | Local Day 2 lab: both ingestion jobs publish to a Redis list. Not used on the VM (ADR-002). |
| `schema.sql` | Database tables: `sensor_readings`, `ingestion_status`, `data_quality_events` |
| `build_training_data.py` | Joins stored NO₂ (RDS) with traffic snapshots (S3), one row per hour, and writes `training_data.csv` |
| `train_model.py` | Trains the `LinearRegression` and writes `model.pkl` and `model_metrics.json` |
| `training_data.csv`, `model.pkl`, `model_metrics.json` | The data, model and metrics currently served (93 hours, 23–27 Nov 2026) |
| `tests/` | pytest suite (19 tests): ingestion, data quality, model and dashboard |
| `infra/` | VM first-boot script (`user-data.sh`) and the `crontab` |
| `docs/` | Architecture design document (GitHub Pages), ADR-001 to ADR-006, and the rebuild runbook |
| `reference/` | The course's helper scripts for Luchtmeetnet and NDW |

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/
```

The tests need no cloud access. To run the jobs against real infrastructure, copy `.env.example` to `.env` and fill in the database and bucket settings. `.env` is gitignored.

## Deploying and retraining

The full procedure for a fresh VM is in [`docs/runbook-rebuild.md`](docs/runbook-rebuild.md). To retrain the model on the VM:

```bash
cd ~/airbreda
docker run --rm --user "$(id -u):$(id -g)" --env-file .env -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/app airbreda-air python build_training_data.py
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work airbreda-dashboard python train_model.py
docker build -t airbreda-dashboard -f Dockerfile.dashboard .
docker rm -f dashboard
docker run -d --name dashboard --restart unless-stopped --env-file .env -p 8000:8000 airbreda-dashboard
```

Training runs inside the dashboard image so that the library versions that write `model.pkl` are the same ones that load it.
