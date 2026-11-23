# AirBreda: architecture design document

> Work in progress. Published via GitHub Pages from `docs/`.

## 1. Architecture diagram

State after Day A (2026-11-23): ingestion only. The dashboard container is added on Day 4.

```mermaid
flowchart LR
  subgraph internet["Public internet (no auth)"]
    LMN["RIVM Luchtmeetnet API<br/>NO₂, station NL10240<br/>hourly JSON"]
    NDW["NDW open data<br/>A27 sites hrl, hrr, vwd, vwa<br/>gzipped DATEX II XML"]
  end

  DEV["Developer laptop"]

  subgraph aws["AWS account, region eu-north-1 (only region allowed by org SCP)"]
    subgraph vpc["Default VPC"]
      subgraph vmsg["airbreda-vm-sg: inbound SSH 22 from one IP only"]
        subgraph vm["EC2 t3.small, Amazon Linux 2023"]
          CRON["cron"]
          AIR["airbreda-air<br/>container, :15 every hour"]
          TRF["airbreda-traffic<br/>container, :00 every hour"]
        end
      end
      subgraph dbsg["airbreda-db-sg: inbound 5432 from airbreda-vm-sg only"]
        RDS[("RDS PostgreSQL 18<br/>db.t3.micro, not public<br/>sensor_readings")]
      end
    end
    ROLE["IAM role airbreda-ec2-role<br/>via instance profile, IMDSv2"]
    S3[("S3 airbreda-raw-701621783246<br/>public access blocked<br/>ndw/YYYY-MM-DD/HH-site.csv")]
  end

  LMN -- "HTTPS GET, last 50 h" --> AIR
  NDW -- "HTTP GET, live feed" --> TRF
  CRON -. "docker run --rm" .-> AIR
  CRON -. "docker run --rm" .-> TRF
  AIR -- "INSERT ... ON CONFLICT DO NOTHING<br/>TLS, password from .env" --> RDS
  TRF -- "PutObject" --> S3
  ROLE -. "temporary credentials" .-> TRF
  DEV -- "SSH (key pair)" --> vm
```

Trust boundaries: the database accepts connections only from the VM's security group, so it is unreachable from the internet and from the laptop. The VM has no AWS access keys; S3 access comes from the instance role. The only secret on the VM is the database password in a `.env` file with mode 600, which is not in git.

## 2. Architecture Decision Records
| ADR | Topic |
|---|---|
| [ADR-001](adr/001-data-storage.md) | Initial Data Storage Strategy |
| [ADR-002](adr/002-messaging.md) | Messaging Architecture |
| [ADR-003](adr/003-resilience.md) | Resilience Strategy |
| [ADR-004](adr/004-compute.md) | Compute Strategy |
| ADR-005 | Compute & Deployment Strategy |
| ADR-006 | ML Serving Architecture |

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
