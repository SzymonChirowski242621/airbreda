# ADR-001: Initial data storage strategy

Status: Accepted
Date: 2026-11-23

## Context

AirBreda ingests two live sources. Luchtmeetnet publishes one NO₂ value per hour for station NL10240. NDW publishes per-minute traffic intensity and speed for four sites at the A27/Breda interchange (hrl, hrr, vwd, vwa), as a gzipped DATEX II file covering the whole country.

The two sources behave differently. Luchtmeetnet can be queried for past hours, so a missed run can be repaired later. NDW only serves the current state. Whatever we do not store at the moment of ingestion is gone, and the regression model on Day 4 needs that traffic history to train on.

The access patterns are also different. The dashboard asks "what is the latest NO₂ for NL10240" and later "give me NO₂ between x and y", which are indexed lookups by station and time. Training asks "give me every traffic reading we have ever collected", which is a bulk read that happens rarely.

The volume is small. One station with one component is 8,760 rows a year. Even 10 stations with three components over five years is about 440,000 rows.

## Decision

Parsed Luchtmeetnet readings go into a managed PostgreSQL database (Amazon RDS, `db.t3.micro`, eu-north-1) in one table:

```sql
sensor_readings (station_id, timestamp, component, value, is_flagged)
PRIMARY KEY (station_id, timestamp, component)
```

The `is_flagged` column exists from the start, because Day 2 flags stale or null readings instead of dropping them.

NDW traffic goes into S3 (`airbreda-raw-701621783246`) as one small CSV per site per hour, under `ndw/YYYY-MM-DD/HH-{site}.csv`. The date and hour come from the measurement timestamp in the data, not from the clock of the machine that ran the job.

Duplicates are handled by making every write idempotent rather than by trying to deliver each reading exactly once. Each air-quality run re-fetches the last 50 hours and inserts with `ON CONFLICT (station_id, timestamp, component) DO NOTHING`, so a repeated run adds nothing and a missed run is repaired by the next one. In S3 the object key is deterministic per site and hour, so re-running the traffic job overwrites the same object instead of creating a second one. This is at-least-once delivery with idempotent writes.

## Alternatives considered

DynamoDB was rejected. It would handle the write volume easily, but so does a single Postgres instance, by a wide margin. The queries we need are time ranges per station and a join between air quality and traffic. DynamoDB can do time ranges with a well-chosen sort key, but joins have to be done in application code, and every new query shape means rethinking the key design. Our schema is fixed and fully known, which is the case where a relational table is the simplest option.

Storing traffic in Postgres as well was also considered and postponed. It would make the Day 4 join a single SQL query. It would also mean a second table to maintain while the only consumer of traffic history is a training script that runs a few times. If the dashboard ever needs historical traffic queries, parsed traffic rows can be loaded from the bucket into a table without re-collecting anything.

## Consequences

The bucket is the only copy of historical traffic, so it has to be treated as the system of record. Public access is blocked, and only the VM's IAM role can write to it.

The CSVs hold the per-site summary (total intensity, average speed, lane count, number of bad values), not the raw XML. That keeps storage tiny: the national measurement file is about 1.2 MB compressed per download, plus 2 MB for the site configuration, while the four CSVs together are under 1 KB. But if the parsing logic turns out to be wrong, past hours cannot be re-parsed. Keeping the raw XML for the four sites would remove that risk and is a cheap change if it becomes necessary.

`DO NOTHING` keeps the first value it sees. RIVM validates measurements after publication, so a value can change later, and with this rule a corrected value would never overwrite the original. If that matters for the model, the fix is `ON CONFLICT ... DO UPDATE SET value = EXCLUDED.value`.

The database is not publicly accessible. Only the VM's security group can reach port 5432, so querying it means connecting through the VM. That is less convenient during development, and it is the reason the database has no internet-facing attack surface.

Day 4's training script has to combine two stores: SQL for NO₂ and a bucket listing for traffic. That is more code than one query, and it is the price of keeping raw traffic history out of the serving database.
