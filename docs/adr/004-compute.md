# ADR-004: Compute strategy

Status: Accepted (extended by ADR-005 on Day 4)
Date: 2026-11-23

## Context

Both ingestion scripts are short batch jobs. The air-quality job takes about a second. The traffic job takes 40 to 60 seconds, almost all of it spent parsing the NDW XML. Each job runs once an hour and exits. On Day 4 a third container, the dashboard, has to run permanently and be reachable on a public port.

The AWS account is a member of the BUas organisation, and an organisation policy allows only the eu-north-1 (Stockholm) region. That rules out the course's suggested eu-west-1, but it still keeps all data inside the EU. The account is on the AWS Free plan, so usage is paid from credits ($100 at the start) rather than billed.

We also need the compute to reach a database that has no public endpoint, and to write to S3 without long-lived access keys on the machine.

## Decision

Run both ingestion jobs as Docker containers on one EC2 instance, scheduled by cron.

The instance is a `t3.small` (2 vCPU, 2 GB RAM) running Amazon Linux 2023, with an 8 GB gp3 root volume. The `t3.micro` with 1 GB is half the price, but building images and running the traffic parser alongside a dashboard container on 1 GB risks running out of memory. Both sizes are free-tier eligible.

S3 access comes from an IAM role attached to the instance (`airbreda-ec2-role`), so the VM holds no AWS keys. The instance requires IMDSv2 for fetching those role credentials. SSH is open only to one IP address. The database security group accepts port 5432 only from the VM's security group.

Cron runs the traffic job at minute 0 and the air-quality job at minute 15, because the hourly NO₂ value appears in the API a few minutes after the hour. Both jobs log JSON lines to files on the VM.

The Redis queue from ADR-002 is not part of this deployment. On a laptop it was a way to exercise the producer, broker and consumer pattern. Here there is one machine and each reading has exactly one destination, so a broker would be one more process to run and monitor with nothing to decouple. Both jobs write straight to RDS and S3. The queue comes back when a reading has more than one consumer that should not be wired into the ingestion code (for example a live anomaly detector next to the database writer), or when ingestion is split across several machines.

## Cost

On-demand prices for eu-north-1, from the AWS Price List API, assuming 730 hours a month:

| Item | Rate | Per month |
|---|---|---|
| EC2 `t3.small` | $0.0216/hour | $15.77 |
| EBS gp3, 8 GB | $0.0836/GB-month | $0.67 |
| Public IPv4 address | $0.005/hour | $3.65 |
| Total for compute | | about $20 |

A `t3.micro` would bring the instance line down to about $7.90. The database is covered in the cost estimate for the full system.

## Alternatives considered

ECS on Fargate, triggered by EventBridge Scheduler, is the managed way to run scheduled containers. We would stop patching an operating system and pay only for the minutes the jobs run, which for two short hourly jobs is less than an always-on VM. We did not choose it today because it needs a container registry, task definitions, a task role and VPC networking for the RDS connection before the first job runs. The dashboard on Day 4 has to be up all the time anyway, so the VM would not go away.

AWS Lambda fits the air-quality job well. The traffic job is close to Lambda's limits in a different way: it downloads and parses several megabytes of XML for close to a minute, and memory, not time, would decide the cost. It would also split the system across two deployment models for no gain at this size.

## Consequences

We own the operating system: updates, disk space, Docker itself, and the cron configuration. Amazon Linux 2023 does not ship with cron, which we found out while setting up the machine. It is now installed by the instance's first-boot script along with Docker, git and the Postgres client.

The VM is a single point of failure. If it stops, ingestion stops, and traffic data for those hours is lost because NDW cannot be queried for the past. Air quality is repaired automatically on the next run, because each run re-fetches 50 hours.

The traffic parser is the scaling bottleneck. The course's reference parser reads the whole national file once per site, so the work grows with the number of sites times the file size. At four sites this costs about a minute of CPU per hour, which a burstable `t3` handles easily.

At 50 corridors (about 200 sites) ingested every 5 minutes, this design breaks in two places. The parser would need far longer than 5 minutes per run, so it has to be rewritten to read the file once and pick out all sites in a single pass. And a run every 5 minutes on a burstable instance would use more CPU than the instance's baseline allows. At that point we would move the jobs to Fargate tasks on EventBridge Scheduler, put a queue between ingestion and the database writer, and keep only the dashboard on long-running compute, or move it to a managed container service as well.

The SSH rule is tied to one IP address, which is the campus network. Working from another network means updating the security group first. That friction is intended.
