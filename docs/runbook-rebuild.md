# Runbook: rebuild AirBreda

This is the recovery procedure behind ADR-003 (backup and restore, RTO 1 hour). Use it when the VM is gone or broken beyond a quick fix, or when the database has to be restored.

Everything runs from the developer laptop with the AWS CLI, in the repository root. All resources live in `eu-north-1`, the only region the account allows.

```bash
export AWS_REGION=eu-north-1
```

## 0. Decide what is broken

| Symptom | Go to |
|---|---|
| VM terminated, unreachable after a reboot, or its disk is corrupted | 1. Rebuild the VM |
| Data in `sensor_readings` is wrong or deleted | 2. Restore the database |
| Both | 2 first, then 1 (the VM needs the new `DB_HOST`) |
| One cron run failed, VM otherwise fine | Not a rebuild. Read `~/airbreda/logs/*.log` on the VM; the next run repairs NO₂ automatically. |

While the traffic job is not running, traffic data for those hours is lost for good (NDW keeps no history). Start with whatever gets ingestion back fastest.

## What survives a VM loss

These live outside the VM and normally need no action:

| Resource | Name |
|---|---|
| Database | RDS `airbreda-db` |
| Bucket | S3 `airbreda-raw-701621783246` |
| Access role | IAM role `airbreda-ec2-role`, instance profile `airbreda-ec2-profile` |
| Firewalls | security groups `airbreda-vm-sg` (sg-09e169281b0a071f7), `airbreda-db-sg` (sg-052fb65829b54f1e0) |
| SSH key | key pair `airbreda-key`, private key `~/.ssh/airbreda.pem` on the laptop |
| Code, first-boot script, crontab | this repository (`infra/user-data.sh`, `infra/crontab`) |
| Secrets | `.env` in the repository root on the laptop (gitignored, mode 600) |

If the laptop's `.env` is also lost, the database password can be reset instead of recovered (step 1.7).

## 1. Rebuild the VM (about 20 minutes)

### 1.1 Log in to AWS

```bash
aws sts get-caller-identity || aws login
```

### 1.2 Make sure SSH is allowed from where you are

The SSH rule allows a single IP address. If you are not on the network it was created from, replace it:

```bash
MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 describe-security-groups --group-ids sg-09e169281b0a071f7 \
  --query 'SecurityGroups[0].IpPermissions[?ToPort==`22`].IpRanges[].CidrIp' --output text
# if the address shown is not $MYIP/32:
aws ec2 revoke-security-group-ingress --group-id sg-09e169281b0a071f7 --protocol tcp --port 22 --cidr <old-ip>/32
aws ec2 authorize-security-group-ingress --group-id sg-09e169281b0a071f7 --protocol tcp --port 22 --cidr $MYIP/32
```

### 1.3 Remove the broken instance (if it still exists)

Look before you delete:

```bash
aws ec2 describe-instances --filters Name=tag:Name,Values=airbreda-vm \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress]' --output table
aws ec2 terminate-instances --instance-ids <old-instance-id>
```

### 1.4 Launch a new instance

```bash
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)

IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type t3.small --key-name airbreda-key \
  --security-group-ids sg-09e169281b0a071f7 --iam-instance-profile Name=airbreda-ec2-profile \
  --user-data file://infra/user-data.sh --metadata-options HttpTokens=required \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=airbreda-vm}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-status-ok --instance-ids "$IID"
IP=$(aws ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "$IID $IP"
```

The new instance gets a **new public IP address**. Anything that points at the old one has to be updated: your SSH commands, the dashboard URL, and on Day 5 the address in the submission form.

### 1.5 Wait for the first-boot script

```bash
ssh -i ~/.ssh/airbreda.pem -o StrictHostKeyChecking=accept-new ec2-user@$IP \
  'cloud-init status --wait; docker --version; systemctl is-active docker crond'
```

Expect `status: done`, a Docker version, and `active` twice. If SSH is refused right after launch, wait a minute and retry.

### 1.6 Install the code and the secrets

```bash
ssh -i ~/.ssh/airbreda.pem ec2-user@$IP \
  'git clone -q https://github.com/SzymonChirowski242621/airbreda.git ~/airbreda && mkdir -p ~/airbreda/logs'
scp -i ~/.ssh/airbreda.pem .env ec2-user@$IP:~/airbreda/.env
ssh -i ~/.ssh/airbreda.pem ec2-user@$IP 'chmod 600 ~/airbreda/.env'
```

If the database was restored in step 2, check that `DB_HOST` in `.env` points at the new endpoint before copying it.

### 1.7 Only if `.env` is lost: reset the database password

```bash
cp .env.example .env && chmod 600 .env
PW=$(python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(28)))")
aws rds modify-db-instance --db-instance-identifier airbreda-db --master-user-password "$PW" --apply-immediately >/dev/null
sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$PW|; s|^S3_BUCKET=.*|S3_BUCKET=airbreda-raw-701621783246|" .env
aws rds describe-db-instances --db-instance-identifier airbreda-db \
  --query 'DBInstances[0].Endpoint.Address' --output text   # put this in DB_HOST
```

Then repeat the `scp` from step 1.6.

### 1.8 Make sure the schema exists, then build

The schema uses `CREATE TABLE IF NOT EXISTS`, so running it on an existing database changes nothing.

```bash
ssh -i ~/.ssh/airbreda.pem ec2-user@$IP 'cd ~/airbreda && set -a && . ./.env && set +a &&
  PGPASSWORD=$DB_PASSWORD psql "host=$DB_HOST user=$DB_USER dbname=$DB_NAME sslmode=require" -q -f schema.sql &&
  docker build -q -t airbreda-air . && docker build -q -t airbreda-traffic -f Dockerfile.traffic .'
```

### 1.9 Run both jobs once, then install the schedule

```bash
ssh -i ~/.ssh/airbreda.pem ec2-user@$IP 'cd ~/airbreda &&
  docker run --rm --env-file .env airbreda-air &&
  docker run --rm --env-file .env airbreda-traffic &&
  crontab infra/crontab && crontab -l'
```

Expect `db_write_success` from the air job and four `fetch_success` lines plus `run_complete` from the traffic job. The air job writes any hours that were missed during the outage, because each run re-reads the last 50 hours.

### 1.10 From Day 4 on: start the dashboard

Once the dashboard exists, also build and start it. `--restart unless-stopped` brings it back after a reboot.

```bash
ssh -i ~/.ssh/airbreda.pem ec2-user@$IP 'cd ~/airbreda &&
  docker build -q -t airbreda-dashboard -f Dockerfile.dashboard . &&
  docker run -d --name dashboard --restart unless-stopped --env-file .env -p 8000:8000 airbreda-dashboard'
```

## 2. Restore the database (about 15 minutes plus copy time)

A restore always creates a **new** RDS instance with a new endpoint; it never overwrites the existing one. That makes it safe to try: the old instance stays until you delete it.

### 2.1 Choose the restore point

- To a moment in the last 24 hours (automated backups are kept for 1 day, the Free plan maximum):

  ```bash
  aws rds describe-db-instances --db-instance-identifier airbreda-db \
    --query 'DBInstances[0].LatestRestorableTime'
  aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier airbreda-db \
    --target-db-instance-identifier airbreda-db-restored --restore-time <ISO-8601 time before the mistake> \
    --db-instance-class db.t3.micro --vpc-security-group-ids sg-052fb65829b54f1e0 --no-publicly-accessible
  ```

- To a manual snapshot (kept until deleted):

  ```bash
  aws rds describe-db-snapshots --db-instance-identifier airbreda-db \
    --query 'DBSnapshots[].[DBSnapshotIdentifier,SnapshotCreateTime]' --output table
  aws rds restore-db-instance-from-db-snapshot --db-snapshot-identifier <snapshot-id> \
    --db-instance-identifier airbreda-db-restored --db-instance-class db.t3.micro \
    --vpc-security-group-ids sg-052fb65829b54f1e0 --no-publicly-accessible
  ```

### 2.2 Wait and point the system at it

```bash
aws rds wait db-instance-available --db-instance-identifier airbreda-db-restored
aws rds describe-db-instances --db-instance-identifier airbreda-db-restored \
  --query 'DBInstances[0].Endpoint.Address' --output text
```

Put that endpoint in `DB_HOST` in the laptop's `.env`, copy it to the VM (step 1.6), and restart the dashboard if it runs. The next cron run writes to the restored database and re-fills NO₂ for the hours since the restore point.

### 2.3 Clean up

When the restored database is confirmed good, take a snapshot of the old one if you might need to look at it again, then delete it. A second RDS instance doubles the database cost.

```bash
aws rds delete-db-instance --db-instance-identifier airbreda-db --final-db-snapshot-identifier airbreda-db-before-restore
```

The restored instance keeps the name `airbreda-db-restored`. Renaming it back to `airbreda-db` is possible with `modify-db-instance --new-db-instance-identifier` but changes the endpoint again, so it is optional.

## 3. Check that recovery worked

```bash
ssh -i ~/.ssh/airbreda.pem ec2-user@$IP 'cd ~/airbreda && docker run --rm --env-file .env airbreda-air python status.py'
```

Recovery is complete when:

- `status` is `ok`, and both `last_successful_fetch` values are from the last few minutes;
- `SELECT MAX(timestamp) FROM sensor_readings` is the latest published hour;
- `aws s3 ls s3://airbreda-raw-701621783246/ndw/$(date -u +%F)/` shows files for the current hour;
- after the next full hour, `~/airbreda/logs/traffic.log` and `air.log` show the scheduled runs;
- from Day 4 on, `http://<new-ip>:8000/health` answers from outside.

Write down how long the recovery took, how many traffic hours were lost, and anything in this runbook that was wrong. ADR-003's one-hour RTO is only real once this has been done for real once.
