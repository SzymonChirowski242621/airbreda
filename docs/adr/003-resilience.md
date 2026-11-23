# ADR-003: Resilience strategy

Status: Accepted
Date: 2026-11-23

## Context

AirBreda serves an air-quality dashboard and a small JSON API (`GET /site/{id}`, from Day 4) for the A27/Breda interchange. Nobody's safety depends on it in real time. A policy officer looking at it during an outage loses a few hours of insight, and RIVM's own Luchtmeetnet site stays available as a fallback. That makes downtime an annoyance, not an incident.

Data loss is a different matter, and it is not the same for both sources:

- NO₂ history can always be re-fetched. Luchtmeetnet serves past hours through `start`/`end` queries, and each ingestion run already re-reads the last 50 hours.
- Traffic history cannot. NDW only publishes the current state, so every hour the traffic job does not run is gone for good. The Day 4 model trains on exactly this data.

The system runs on single components in one region: one EC2 instance, one single-AZ RDS instance, one S3 bucket. The BUas organisation policy allows only eu-north-1, so a second region is not available as a disaster-recovery target at all.

AWS's own service commitments set a ceiling on what one instance of each can promise. A single EC2 instance and a single-AZ RDS instance are each covered at 99.5% monthly uptime. The API needs both at the same time, so the combined expectation is about 99.5% × 99.5% ≈ 99.0%.

The December 2021 us-east-1 outage is the reference case for a regional failure. It lasted about five hours, and services that were designed to be independent went down together because they shared one region's internal network.

## Decision

### SLO

`GET /site/{id}` gets an availability SLO of **99%**, measured per calendar month.

The SLI is the fraction of probes, one every 5 minutes for each of the four sites, that return HTTP 200 within 2 seconds with a non-null `no2_ug_m3` and `intensity_veh_per_hr`. A response with stale data counts as good for availability. Freshness is tracked separately through `/health`, which reports `degraded` when a source has not succeeded for two hours.

99% leaves an error budget of 432 minutes (7.2 hours) a month. 99.5% (216 minutes) would match what each single component is promised on its own, and the two together cannot keep that promise. 99.9% (43 minutes) would need redundant compute and a Multi-AZ database for an internal dashboard that nobody depends on minute by minute.

### Disaster-recovery tier

**Backup and restore**, inside eu-north-1:

- The database uses RDS automated backups with point-in-time restore, kept for 1 day. Seven days was the intended setting, but the AWS Free plan refuses any retention above its free-tier maximum (`FreeTierRestrictionError`), so 1 day is what this account allows. Manual snapshots are allowed and are kept until deleted: one is taken before each risky change (the first, `airbreda-db-day-b-2026-11-23`, before Day C's model work). Backup storage up to the size of the database (20 GB) is free.
- S3 stores the traffic history. It is replicated across availability zones by default and needs no extra backup at this size.
- The VM is not backed up. It holds no state: the code is in git, the first-boot script installs Docker, cron, git and the Postgres client, and the only secret is the `.env` file, which is recreated from the password manager. Rebuilding it means launching a new instance with the same script, cloning the repo, copying `.env`, building two images and installing the crontab.

Targets:

| | Target | Reasoning |
|---|---|---|
| RTO | 1 hour | A new instance was ready for Docker in about 3 minutes on Day A, and both images build in about 2 minutes. The rest is a person following the runbook. |
| RPO, NO₂ | 0 | Re-fetched from Luchtmeetnet after recovery. |
| RPO, database | 5 minutes | Point-in-time restore of RDS. |
| RPO, traffic | the length of the outage | NDW has no history. No DR tier changes this; only keeping ingestion running does. |

## Cost compared with the tiers above

Current monthly cost at eu-north-1 on-demand prices: compute $20.09 (t3.small, disk, public IP) and database $16.27 (db.t3.micro at $0.019/hour plus 20 GB gp3 at $0.12/GB), about **$36** in total. Backup and restore adds almost nothing.

| Tier | What changes | Extra per month | RTO |
|---|---|---|---|
| Backup and restore (chosen) | 1-day RDS backups, manual snapshots, rebuild runbook | about $0 | 1 hour |
| Pilot light | RDS Multi-AZ ($0.037/hour, storage $0.24/GB) and a stopped standby VM in a second AZ (disk and IP only) | about +$20 (+55%) | 10–15 minutes |
| Warm standby | RDS Multi-AZ, a second running VM in another AZ, an Application Load Balancer ($0.02394/hour) | about +$53 (+146%) | a few minutes |
| Multi-region active-active | second region | not possible: blocked by the organisation policy | n/a |

Prices are from the AWS Price List API for eu-north-1, assuming 730 hours a month. The load balancer is counted without capacity units, which at this traffic would add cents.

The step to pilot light would cost about $240 a year to cut recovery from an hour to about fifteen minutes. With a 7.2-hour monthly budget, one restore uses 14% of it. That is not worth $240 a year for this dashboard.

## Alternatives considered

The course text suggests warm standby with a 15-minute RTO and a 1-hour RPO. In this account a warm standby could only live in a second availability zone of the same region. It would protect against an instance or AZ failure, but not against a regional outage like the one in 2021. It also would not lower the traffic RPO, which is the only loss that matters here. It would more than double the monthly cost to protect the part of the system that is easy to rebuild.

## Consequences

A regional outage takes AirBreda down for as long as the region is down, and traffic for those hours is lost. A five-hour outage would use 300 of the 432 minutes in that month's budget (69%). Under a 99.5% SLO the same outage would blow that month's budget, and it would use about 11% of the year's budget (43.8 hours).

One person must be able to run the rebuild. The runbook has to be written down and tried once, or the one-hour RTO is a guess. Infrastructure as code (the Extension session) would make the rebuild one command and is the first thing to add if the RTO needs to shrink.

The SLO is defined before there is anything to measure. The probe described above has to exist by Day 4, or the SLO stays on paper.

The 1-day backup window is acceptable here for two reasons. NO₂ history can be re-fetched from Luchtmeetnet for any period, so an old database backup protects nothing that the source does not already keep. Traffic history lives in S3, not in the database, so RDS backups do not cover it either way. What the short window does not cover is a mistake inside the database, such as a wrong `DELETE`, noticed more than a day later; manual snapshots before risky changes close most of that gap. On a paid account the retention would be 7 days, which is still free up to 20 GB.

The rebuild runbook is not written yet.
