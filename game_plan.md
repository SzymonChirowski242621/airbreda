# System Design & Cloud Platforms — 3-Day Game Plan

Source: [course page](https://adsai.buas.nl/Electives/SystemDesignCloudPlatforms/) (Days 1–5 read on 2026-11-23).

The course runs 5 days: Days 1–4 are build days and Day 5 is the evaluation (individual, deadline 17:00, no help).
This plan fits Days 1–4 into **3 working days**, so that evaluation day is only for polishing and submitting.

| Deliverable | Weight | What gets checked |
|---|---|---|
| Live Deployed System | 60% | `GET /site/{hrl,hrr,vwd,vwa}`, `GET /health`, `GET /` with real data, your own model, both bad-data handlers, structured JSON logs |
| Architecture Design Document | 40% | GitHub Pages, 6–8 pages: diagram, ADR-001…006, trade-offs, explanation for the municipality, cost table (1/10/50 corridors), reflection |

## The constraint that drives the plan

The NDW traffic feed is **live only**, so you can't download past traffic data.
The Day 4 regression can only train on the hours your own pipeline has been collecting.
**Every hour without hourly cron on the cloud VM is training data you don't get back.**
So the VM setup moves from course Day 3 into Day A, and the theory fits in around it.

---

## Day A — Get data flowing (course Day 1 + Day 3)

**Goal by evening:** hourly cron on a cloud VM writes NO₂ rows to the database and NDW CSVs to the bucket.

### Morning — setup + Lab 1
- [ ] Pick a provider (AWS or Azure) and stick with it. Azure for Students usually needs no credit card.
- [ ] Authenticate the CLI (`aws sts get-caller-identity` / `az account show`), run `docker run hello-world`
- [ ] **Set a €10 billing alert now**
- [ ] Run `extract.py` + the helper scripts, then answer the Lab 1 questions (fields, time resolution, unknown formula, how fresh the data is)
- [ ] `ingest_air.py`: latest NO₂ for NL10240, timeout/error handling, **CAP trade-off comment**
- [ ] `Dockerfile` → `airbreda-air`
- [ ] `tests/test_ingest_air.py`: null NO₂ rows are kept, not dropped

### Midday — Lab 2
- [ ] Managed Postgres (RDS / Azure). Create `sensor_readings` with **`is_flagged BOOLEAN DEFAULT FALSE` included from the start**
- [ ] Idempotent writes: `INSERT ... ON CONFLICT DO NOTHING`
- [ ] Object storage bucket (lowercase name, `eu-west-1` / `westeurope`)
- [ ] `ingest_traffic.py`: reuse `download_and_decompress`, `build_index_map`, `extract_measurements`; save `ndw/YYYY-MM-DD/HH-{site}.csv` (timestamp taken from the measurement, not from today's date), then upload
- [ ] **Required comment:** why both the database and the bucket (retraining in 6 months)
- [ ] `Dockerfile.traffic` → `airbreda-traffic`

### Afternoon — VM + cron (course Day 3)
- [ ] VM: t3.micro (Amazon Linux 2023) / Standard_B1s (Ubuntu 24.04), SSH from **your IP only**
- [ ] Install Docker, add the user to the `docker` group
- [ ] IAM instance role / managed identity for the bucket (**no copied credentials**)
- [ ] Database firewall/security group allows the **VM's IP**
- [ ] `git clone` the repo on the VM, put secrets in `.env` (and in `.gitignore`), build both images
- [ ] Crontab with absolute docker paths:
  ```
  0 * * * * /usr/bin/docker run --rm --env-file /home/<user>/airbreda/.env airbreda-air
  0 * * * * /usr/bin/docker run --rm --env-file /home/<user>/airbreda/.env airbreda-traffic
  ```
- [ ] **Check:** after the next full hour, `SELECT COUNT(*) FROM sensor_readings` > 0 and the bucket listing > 0

### Evening — light
- [ ] Reading: Zalando, CAP (ByteByteGo + Kleppmann), Spotify Part I/II, storage types, Lambda vs Kappa
- [ ] First architecture diagram
- [ ] **ADR-001** Initial Data Storage Strategy
- [ ] **ADR-004** Compute Strategy (write it while the VM details are fresh: cost per month, what you'd use at 50 corridors / 5 min, one surprise)
- [ ] Optional: check whether the Luchtmeetnet API takes `start`/`end` parameters for past NO₂ readings. This does **not** fix the traffic side.

---

## Day B — Patterns, logging, data quality (course Day 2)

**Goal:** the graded bad-data handlers and structured logging, running on the VM.

### Redis lab — keep it short (~1h)
- [ ] `docker-compose.yml` with `redis` + `air-ingest` + `traffic-ingest` on the `airbreda` network
- [ ] `redis-cli -h redis ping` → PONG from both containers
- [ ] `rpush("readings", ...)` from both scripts, check the list with `LRANGE readings 0 -1`
- [ ] Comment on the polling interval (hourly vs every minute)
- It's only there to give ADR-002 something to describe. Course Day 3 drops Redis again, so don't polish it.

### Lab 2 — the part that's graded
- [ ] JSON logs to stdout (`logging`), INFO `fetch_success` per source / per NDW site
- [ ] Luchtmeetnet stale (same value for 3+ hours) or null → WARNING `DATA_QUALITY_ERROR` → **write the row with `is_flagged=TRUE`**
- [ ] NDW `speed=-1` → WARNING `DATA_QUALITY_ERROR` → **skip the write**
- [ ] Bad-data counters per source; > 10/hour → one ERROR `BAD_DATA_THRESHOLD_EXCEEDED`
- [ ] `tests/test_data_quality.py`: one test for flag-and-keep, one for drop-and-count
- [ ] **Design fix:** cron containers exit after each run (`--rm`), so in-memory counters and a per-container `/health` don't last. Store `last_successful_fetch` + `bad_data_count` per source in a small table, e.g. `ingestion_status`. The Day C `/health` reads real values from it.
- [ ] Rebuild the images on the VM, check that cron still produces rows

### Docs
- [ ] Reading: Shopify modular monolith, DHH, event-driven, Nygard ADRs, AWS Dec 2021 outage, SRE SLOs
- [ ] Update the diagram (queue, logging path, data-quality path)
- [ ] **ADR-002** Messaging Architecture (why a queue, why Redis, what if the broker goes down, why flag-and-keep vs drop)
- [ ] **ADR-003** Resilience Strategy (SLO 99 / 99.5 / 99.9, DR tier, cost of the next tier up)

---

## Day C — Model, dashboard, deploy (course Day 4)

**Goal:** every graded endpoint works at `http://<vm-ip>:8000`.

### Morning — model
- [ ] `build_training_data.py`: database NO₂ + bucket CSVs → joined per hour, `total_intensity_veh_per_hr`, `hour_of_day` → `training_data.csv`
- [ ] Row count + scatter plot of NO₂ vs intensity. If there are fewer than 24 rows, write that down in ADR-006; don't hide it
- [ ] `LinearRegression` → `model.pkl`; report R², MAE, `coef_` (a wrong-signed coefficient goes in the reflection)
- [ ] Exceedance threshold (WHO/EU values, note that those are annual averages) → sigmoid risk score 0–1
- [ ] Stretch: `LogisticRegression` on an exceedance label and compare it with the sigmoid
- [ ] `predict.py` → `{"no2_ug_m3_predicted", "no2_exceedance_risk"}`
- [ ] `tests/test_model.py`: pkl loads, NO₂ prediction is in 0–200, risk is in 0–1

### Midday — dashboard
- [ ] `dashboard.py` (FastAPI):
  - `GET /site/{site_id}` → `site_id, no2_ug_m3, intensity_veh_per_hr, no2_exceedance_risk, timestamp`
  - `GET /health` → `{"status": "ok", "luchtmeetnet": {...}, "ndw": {...}}`
  - `GET /` → calls its own 4 `/site/*` endpoints: actual NO₂, predicted NO₂ + risk per site, total congestion
- [ ] Auto-refresh (`setInterval` or meta refresh) + "last updated" indicator
- [ ] **Required comment:** where each field comes from + what happens if `predict()` raises an error (recommended: return the real values without a prediction)
- [ ] `Dockerfile.dashboard` (bakes in `model.pkl`), test it with local compose against the real cloud database and bucket

### Afternoon — deploy + security
- [ ] Deploy to the VM: `docker run -d --restart unless-stopped --env-file .env -p 8000:8000 airbreda-dashboard`
- [ ] Open port 8000 to **everyone**, not just your IP, so the grader can reach it
- [ ] Narrow IAM: `s3:PutObject`/`s3:GetObject` on your bucket ARN only (or Blob Data Contributor scoped to your storage account), then check the logs for failed writes
- [ ] Open all 6 URLs from your laptop

### Evening — document
- [ ] Final diagram: all 3 containers on the VM, bucket, database, where the model lives, trust boundaries / IAM
- [ ] **ADR-005** Compute & Deployment (extends or replaces ADR-004? what happens on reboot, what compose caught)
- [ ] **ADR-006** ML Serving (how much data before a bigger model makes sense, how risk is derived, training–serving mismatch, why one station is enough for 4 sites, behaviour when `predict()` fails)
- [ ] `docs/` → GitHub Pages turned on
- [ ] Draft the cost table (1 / 10 / 50 corridors) from the official price calculator, plus a note on whether one VM still fits
- [ ] Draft the explanation for the municipality (250–400 words, no jargon)

---

## Evaluation day (Day 5) — polish + submit before 17:00

- [ ] Reflection (400–600 words): decision you're least confident in, the model with a year of data, the first production addition
- [ ] Trade-off paragraph per decision (storage, compute, messaging, DR) with AirBreda numbers
- [ ] Every ADR is Context → Decision → Consequences, not bullet lists
- [ ] `git log -p | grep -i password` comes back clean
- [ ] Endpoint checklist: 4× `/site`, `/health` with real timestamps, `/` populated, both handlers active
- [ ] Submit: GitHub Pages URL, VM `http://<ip>:8000`, repo URL
- [ ] **Delete everything within 48h** (VM, database, emptied bucket, NAT gateway)

## Risks

- **Day A runs over:** still get the VM and cron running that evening, even with rough scripts. You can improve the code later; you can't get lost hours of data back.
- **1 GB RAM VM:** building images while containers are running can run out of memory. Build one image at a time.
- **Grader can't connect:** the security group only allows your IP on 8000. Check from your phone's mobile data.
- **Dashboard shows blank values:** check `.env` first, then the database firewall (VM IP), then bucket permissions.
- **Cost:** leftover RDS instances and NAT gateways after the course. The billing alert from Day A catches them.

---

## Reading

Most readings can be skipped. The course pages already summarise them in their "key takeaways" boxes, and the grade comes from the system and the ADR reasoning.

### Worth actually reading (~1h total)

| # | Reading | Time | Why | Feeds |
|---|---|---|---|---|
| 1 | Michael Nygard, *Documenting Architecture Decisions* | 5 min | The original ADR post. Six ADRs are a big part of the 40%; this shows the right tone: short, honest, focused on consequences | All ADRs |
| 2 | Google, *Rules of Machine Learning*: "Overview" + "Before Machine Learning" | 15 min | The argument that a simple model is right when you have little data | ADR-006, reflection |
| 3 | AWS Service Event Summary, December 2021 (us-east-1) | 15 min | A concrete case to cite: one region failed, and services designed to be independent still went down together | ADR-003 |
| 4 | Google SRE Workbook, SLO chapter (skim) | 15 min | Error-budget calculation: 99.5% ≈ 3.6 h of downtime/month | ADR-003 |
| — | *Optional:* Kleppmann, *Please stop calling databases CP or AP* | 15 min | Interesting if you like theory; not needed | ADR-001 |

### TL;DR of the rest

**Day 1 — fundamentals & storage**
- **Zalando cloud migration:** moving to the cloud is a design decision, not just an infrastructure task. Cost surprises hit teams that had never thought about compute billing. Using several clouds multiplies operational complexity (different IAM, networking and SLAs).
- **CAP theorem:** network partitions will happen, so the real choice is CP (consistent, may be unavailable) vs AP (available, may be stale). Luchtmeetnet behaves like an AP system: it returns stale or null values instead of failing → your pipeline flags those rows.
- **IaaS / PaaS / SaaS:** the more the provider manages, the less control you have and the less operations work you do. Your VM is IaaS: full control, full responsibility (OS, Docker, patches).
- **Pricing models:**
  - On-demand: unpredictable load, dev/test.
  - Reserved: steady load, up to 66% off.
  - Spot: jobs that can be interrupted, 70–90% off.
  - Savings Plans: a flexible commitment to a $/hr spend.
- **FinOps:** every engineer owns cost. Measure usage, then size resources correctly, and only then commit to reserved pricing. Exercise answer: a €500/mo cluster used 12 of 24 hours wastes ~€250/mo → fix it with scheduled scale-to-zero or autoscaling.
- **Spotify event delivery:** one Postgres worked until it didn't. Read replicas → replication lag; sharding → queries spread across shards. Kafka let them scale producers and consumers separately → object storage → BigQuery. At AirBreda's ~96k rows/year, one Postgres is clearly enough.
- **Storage types:**
  - Object storage: raw files, backups, ML datasets.
  - Block storage: databases, OS disks.
  - File storage: shared NFS/SMB filesystems.
  - For AirBreda: raw NDW files go in the bucket, parsed readings in the database.
- **SQL vs NoSQL (TimescaleDB blog):** sensor data is structured, queried by time range, and air must be joined with traffic → SQL. NoSQL wins when the schema keeps changing, for huge write throughput on simple keys, or for graph data.
- **Lambda vs Kappa (Databricks, Jay Kreps):** Lambda runs a batch layer + a speed layer → two codebases that drift apart. Kappa uses one stream; you reprocess by replaying it. Kappa suits hourly data better.
- **Delivery guarantees (Confluent, Flink):** at-most-once / at-least-once / exactly-once. Use at-least-once with idempotent upserts on `(station_id, timestamp, component)`. Exactly-once is over-engineering here.
- **Well-Architected storage guides:** pick the store from the access pattern, not from habit.

**Day 2 — patterns & resilience**
- **Shopify modular monolith / DHH *Majestic Monolith*:** Shopify ran $100B+ in sales on a monolith. Microservices solve the problem of coordinating large teams, not a technical problem. The real question is how tightly to couple things at your team size.
- **Martin Fowler, *Microservices*:** services can be deployed separately and failures stay contained, but you now run a distributed system. Two ingestion services sharing one Compose setup is enough; no service mesh.
- **Event-driven architecture (AWS / Azure):** producer → broker → consumer. You can add a new consumer (e.g. an anomaly detector) without changing the producer. A queue gives each message to one consumer; a topic gives every consumer a copy.
- **ADR GitHub org:** templates. Use Context → Decision → Consequences.
- **The Verge on the 2021 outage:** what users saw (Netflix, Disney+). The post-mortem above is the better source.
- **Observability vs monitoring:**
  - Metrics: is it healthy now?
  - Logs: what happened?
  - Traces: how did a request move through the system?
- **Structured logging (Well-Architected):** JSON events that can be queried and alerted on, not `print()` strings.
- **AirBreda DR conclusion:** Warm Standby (15-min recovery time, 1-h data-loss window) is enough; active-active isn't worth the cost.

**Day 3 — DataLab**
- No readings. Lessons from the course text: use IAM roles / managed identities instead of copied credentials, keep secrets in `.env` (not in `-e` flags or shell history), give cron absolute paths, and drop the queue once there's only one consumer.

**Day 4 — ML systems**
- **Chip Huyen, *Designing ML Systems*:** background only. The key idea is training–serving skew: features computed differently in training and in serving. Baking `model.pkl` into the image avoids version drift, but every retrain needs a rebuild + redeploy.

**Skip entirely:** the Kleppmann and Huyen textbooks (background), and the Netflix / Spotify / Airbnb / Uber / Shopify / Cloudflare engineering blogs (background only).
