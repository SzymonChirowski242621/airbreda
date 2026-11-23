# AirBreda

Cloud pipeline for the BUas *System Design & Cloud Platforms* elective: ingests live NO₂ (RIVM Luchtmeetnet, station NL10240)
and traffic (NDW, four A27/Breda sites), stores it on AWS (eu-north-1), and serves a regression-based NO₂ prediction dashboard.

- Architecture document: `docs/` (GitHub Pages)
- Course helper scripts: `reference/`

## Local setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in
.venv/bin/pytest tests/
```
