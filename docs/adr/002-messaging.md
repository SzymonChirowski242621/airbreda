# ADR-002: Messaging architecture

Status: Accepted for the local lab. Not part of the cloud deployment (see ADR-004).
Date: 2026-11-23

## Context

On Day A both ingestion scripts wrote straight to their stores: NO₂ into Postgres, traffic into S3. That works while each reading has exactly one destination. The Day 4 plan adds a second use for the same readings (the model), and an obvious third one is an anomaly detector that watches for sudden NO₂ jumps. If each new consumer has to be wired into the ingestion code, every addition means changing and redeploying the scripts that collect the data, which are the parts we can least afford to break: a broken traffic job loses data permanently, because NDW has no history.

The load is tiny. One run per hour produces at most 5 new messages (one NO₂ reading, four traffic sites), about 3,700 a month.

The two sources also need different handling for bad values, and that rule has to hold no matter which path a reading takes.

## Decision

Readings are published as JSON messages onto a shared broker, and consumers read from the broker instead of being called by the producers. For the lab the broker is a Redis list called `readings`, run as a third container next to the two ingestion containers in `docker-compose.yml`. Each script pushes one message per reading with `RPUSH`:

```json
{"station_id": "NL10240", "timestamp": "2026-11-21T11:00:00+00:00", "component": "NO2", "value": 37.93, "is_flagged": false}
{"site": "hrl", "site_id": "RWS01_MONIBAS_0271hrl0063ra", "timestamp": "2026-11-23T12:43:00Z", "intensity_veh_per_hr": 2220.0, "avg_speed_kmh": 99.0}
```

Publishing is optional and switched on by `REDIS_HOST`. A failed publish is logged as `queue_publish_failed` and never fails the run. When a database is present, the air job publishes only rows that are new or whose flag changed, so its 50-hour re-fetch window does not flood the queue with repeats.

Redis was chosen for the lab because it runs as one container with no cloud setup, which makes the pattern visible within minutes. It is not the production choice. If AirBreda needs a broker in the cloud, it would be Amazon SQS: a managed queue with durable storage, at-least-once delivery, a visibility timeout so a message that a crashed consumer took is delivered again, and a dead-letter queue for messages that keep failing. At 3,700 messages a month it stays inside the 1 million requests a month that SQS includes for free. If several consumers each need their own copy, SNS in front of one SQS queue per consumer gives fan-out without the consumers competing for messages.

The cloud deployment from Day 3 onward does not use a broker. ADR-004 explains why: one VM and one destination per reading leave nothing to decouple.

## What happens when the broker is down

We tested this by stopping the Redis container and running the air job. It logged:

```json
{"event": "queue_publish_failed", "source": "Luchtmeetnet", "queue": "readings", "error": "Error -3 connecting to redis:6379. Temporary failure in name resolution."}
```

and finished with exit code 0. The direct database write is unaffected, so in the current design nothing is lost; only the queue copy is missing.

If the broker were the only path to the database, the consequences would differ per source. Lost NO₂ messages are recovered on the next run, because the air job re-fetches 50 hours and the database write is idempotent. Lost traffic messages cannot be recovered from NDW, so the traffic job would have to keep writing its CSV to S3 before publishing, and the S3 file would stay the system of record.

Redis as configured here also loses messages in two quieter ways. It runs without persistence, so restarting the container empties the list. And a consumer that takes a message with `LPOP` and then crashes has lost it, because a plain list has no acknowledgement. Both are reasons it stays a lab tool. SQS covers both by design.

## Why Luchtmeetnet bad data is flagged and kept while NDW bad data is excluded

NO₂ is the value the model predicts. A missing hour in that series is harder to deal with than a marked one: a join on the hour silently drops it, and the gap is invisible later. So a null or stale NO₂ reading is stored, with `is_flagged = TRUE`, and the model can decide to skip it.

NDW's `-1` is a sentinel, not a measurement. It means one lane at one site had no valid reading that minute. Storing `-1` as a speed would pull every average down and break any query that does not know about it. So the value is left out of the site's totals, logged, and recorded in `data_quality_events`.

Looking at it again, the two rules are closer than they first seem. In both cases the record survives and the bad value does not end up in a numeric field: the NO₂ row is kept with `value = NULL` and a flag, and the traffic row is kept with the lane left out and `bad_value_count` set. Starting over, I would make that explicit and apply one rule to both sources: keep the record, never store a sentinel as a number, and mark the record as flagged. For traffic that means an `is_flagged` column in the CSV next to `bad_value_count`, so training code can filter both sources the same way.

## Consequences

Adding a consumer later means subscribing to the broker, with no change to the ingestion code. Today that benefit is unused, because the deployment has no broker and no second consumer yet.

The scripts now carry a small optional publishing path (`broker.py`, the `redis` package) that does nothing on the VM. It is kept because it is cheap and documents the message format a future consumer would read.

Every consumer has to tolerate duplicates. Both Redis and SQS deliver at least once, so consumers must write idempotently, the same way the air job already does with its upsert on `(station_id, timestamp, component)`.

The broker becomes worth deploying when there is a second consumer that should not live inside the ingestion code, or when ingestion is split across more than one machine. At that point it would be SQS, not Redis.
