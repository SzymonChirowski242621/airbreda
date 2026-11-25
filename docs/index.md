# AirBreda: architecture design document

> Work in progress. Published via GitHub Pages from `docs/`.

## 1. Architecture diagram

The system as deployed on 2026-11-25: two hourly ingestion jobs and a dashboard container on one VM, a private database, and a bucket for traffic history.

```mermaid
flowchart LR
  subgraph internet["Public internet"]
    LMN["RIVM Luchtmeetnet API<br/>NO₂, station NL10240"]
    NDW["NDW open data<br/>A27 sites hrl, hrr, vwd, vwa"]
    USER["Browser / grader"]
  end

  DEV["Developer laptop"]

  subgraph aws["AWS, eu-north-1 (only region allowed by org policy)"]
    subgraph vmsg["airbreda-vm-sg: 8000 from anywhere, 22 from one IP"]
      subgraph vm["EC2 t3.small"]
        CRON["cron"]
        AIR["airbreda-air<br/>:15 every hour"]
        TRF["airbreda-traffic<br/>:00 every hour"]
        DASH["airbreda-dashboard<br/>FastAPI :8000, always on<br/>model.pkl baked in"]
      end
    end
    subgraph dbsg["airbreda-db-sg: 5432 from airbreda-vm-sg only"]
      RDS[("RDS PostgreSQL 18, not public<br/>sensor_readings<br/>ingestion_status<br/>data_quality_events")]
    end
    ROLE["IAM role airbreda-ec2-role<br/>Get/Put on ndw/*, List on ndw/ only"]
    S3[("S3 airbreda-raw-701621783246<br/>public access blocked<br/>ndw/YYYY-MM-DD/HH-site.csv")]
  end

  LMN -- "last 50 h" --> AIR
  NDW -- "live feed" --> TRF
  CRON -. "docker run --rm" .-> AIR
  CRON -. "docker run --rm" .-> TRF
  AIR -- "upsert readings, flags, status" --> RDS
  TRF -- "status, bad values" --> RDS
  TRF -- "PutObject" --> S3
  DASH -- "latest NO₂, health" --> RDS
  DASH -- "latest CSV per site" --> S3
  ROLE -. "temporary credentials" .-> TRF
  ROLE -. "temporary credentials" .-> DASH
  USER -- "HTTP GET /, /site/{id}, /health" --> DASH
  DEV -- "SSH (key pair)" --> vm
```

Trust boundaries:

- The database accepts connections only from the VM's security group. It is unreachable from the internet and from the laptop; development access goes through an SSH tunnel via the VM.
- The VM holds no AWS access keys. S3 access comes from the instance role, which can read and write objects under `ndw/` and list that prefix, and nothing else.
- The only secret on the VM is the database password, in a `.env` file with mode 600 that is not in git.
- Port 8000 is public so the dashboard can be graded. The app is read-only and serves only open data.

The model is trained offline (`build_training_data.py` on the VM, then `train_model.py` in the dashboard image) and shipped inside the dashboard image; see ADR-006.

## 2. Architecture Decision Records
| ADR | Topic |
|---|---|
| [ADR-001](adr/001-data-storage.md) | Initial Data Storage Strategy |
| [ADR-002](adr/002-messaging.md) | Messaging Architecture |
| [ADR-003](adr/003-resilience.md) | Resilience Strategy |
| [ADR-004](adr/004-compute.md) | Compute Strategy |
| [ADR-005](adr/005-deployment.md) | Compute & Deployment Strategy |
| [ADR-006](adr/006-ml-serving.md) | ML Serving Architecture |

Operations: [rebuild runbook](runbook-rebuild.md) (recovery procedure for ADR-003).

## 3. Trade-off justifications
## 4. Cloud provider rationale (municipal audience)
## 5. Cost estimate
## 6. Reflection

<!-- GitHub Pages (Jekyll) does not render Mermaid by itself; this turns mermaid code blocks into diagrams. -->
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
  // Jekyll + Rouge wraps fenced blocks as <div class="language-mermaid ..."><div><pre><code>;
  // plain kramdown uses <pre><code class="language-mermaid">. Handle both.
  document.querySelectorAll("div.language-mermaid, pre > code.language-mermaid").forEach((el) => {
    const block = el.tagName === "CODE" ? el.parentElement : el;
    const div = document.createElement("div");
    div.className = "mermaid";
    div.textContent = block.textContent;
    block.replaceWith(div);
  });
  mermaid.initialize({ startOnLoad: false });
  await mermaid.run();
</script>
