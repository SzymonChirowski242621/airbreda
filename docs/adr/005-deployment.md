# ADR-005: Compute and deployment strategy

Status: Accepted. Extends ADR-004.
Date: 2026-11-25

## Context

Day 4 adds a third container: a FastAPI dashboard that serves `GET /site/{id}`, `GET /health` and an HTML page at `/`. Unlike the two ingestion jobs, which run for a minute an hour and exit, the dashboard has to be up all the time and reachable from outside, because the Day 5 grader opens it at the VM's public address.

The dashboard needs the same access as the ingestion jobs: read from RDS, which only accepts connections from the VM's security group, and read from the S3 bucket.

The VM from ADR-004 is a `t3.small` with 2 GB of memory. With the dashboard running and no job active it uses about 310 MB.

## Decision

Run the dashboard as a third container on the same VM, next to the two cron jobs:

```bash
docker run -d --name dashboard --restart unless-stopped --env-file .env -p 8000:8000 airbreda-dashboard
```

`--restart unless-stopped` makes Docker start the container again after a crash or a reboot, and Docker itself is enabled as a system service. We tested this by rebooting the VM: the dashboard answered `/health` from outside 21 seconds after the reboot command. The two ingestion containers do not need a restart policy, because they are not long-running. cron is enabled as a service too, so the next scheduled run starts them as usual.

Port 8000 is open to `0.0.0.0/0` in `airbreda-vm-sg`, so the grader can reach the dashboard. SSH stays limited to one IP address.

The instance role's broad `AmazonS3FullAccess`, used to get started on Day 3, is replaced by an inline policy that allows exactly what the code does:

| Action | Resource | Used by |
|---|---|---|
| `s3:PutObject`, `s3:GetObject` | `arn:aws:s3:::airbreda-raw-701621783246/ndw/*` | traffic job writes; dashboard and training build read |
| `s3:ListBucket` | the bucket, only for prefixes `ndw/*` | dashboard finds the latest file per site; training build lists all files |

After the change, the traffic job still uploaded its four files and the dashboard still served real values. Listing the bucket root, writing an object outside `ndw/` and listing all buckets were each refused with `AccessDenied`.

## Why not a managed container service

ECS on Fargate, or App Runner, would run the dashboard without an operating system to maintain and could replace a failed task automatically. We did not move to one because the VM already exists, is already allowed into the database security group, and has spare memory. A managed service would mean a container registry, a task definition, a task role, a load balancer or public task IP, and a security group rule from the service into RDS. That is a day of infrastructure work for a dashboard with a handful of users, and the ALB alone (about $17.50 a month in eu-north-1, see ADR-003) would cost about as much as the VM.

What would change this: a second instance for availability (the ADR-003 warm-standby tier needs a load balancer anyway, at which point Fargate behind it is a small step), traffic high enough that the dashboard competes with the parsing job for CPU, or a team that should not need SSH to deploy.

## Local testing before the VM

The course suggests running all three containers with Docker Compose against the real cloud database before deploying. That does not work in this design: the database has no public endpoint (ADR-001), so a container on the laptop cannot reach it. Instead the dashboard image ran on the laptop against the real stores, with the database reached through an SSH tunnel via the VM and S3 through short-lived credentials from the developer login. All four `/site` routes, `/health` and `/` returned real data, and an unknown site returned 404.

The local run did not surface any bug. Two problems showed up during deployment instead:

- The training-data script had run in a container as root and left a root-owned `training_data.csv` in the project folder on the VM, which blocked the next copy. Scripts that write into a mounted folder should run with `--user $(id -u):$(id -g)`.
- A code review before deployment found that `with psycopg2.connect() as conn` commits but does not close the connection. In a long-running dashboard that would leak one connection per request until RDS refused new ones. The dashboard now closes connections explicitly.

## Relation to ADR-004

This extends ADR-004 and does not reverse it. The compute choice is the same: one `t3.small` VM running Docker. What changed is that the VM now runs one long-lived container in addition to the two scheduled jobs, exposes a public port, and has a narrower IAM policy. The ADR-004 condition for leaving the VM (50 corridors at 5-minute ingestion) still holds, and so does its cost of about $20 a month for compute.

## Consequences

The VM is now also the single point of failure for the API. If it stops, both ingestion and the dashboard stop together. ADR-003 accepts this within a 99% SLO.

Deploying a change to the dashboard is a manual SSH step: copy or pull the code, rebuild the image, replace the container. That takes a few minutes and briefly drops requests. A CI/CD pipeline (Extension session) would remove the manual step.

The dashboard port is public. The app is read-only, has no authentication and exposes only data that is open anyway, so the risk is mainly load: someone hammering `/site` would hit the 60-second cache rather than the database. There is no TLS on port 8000; the grader's URL is plain HTTP.

Retraining the model means rebuilding the dashboard image, because `model.pkl` is baked into it (see ADR-006).
