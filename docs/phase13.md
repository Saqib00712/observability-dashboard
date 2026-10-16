# Phase 13 — Docker and local deployment

Docker Compose is used because it gives one-command start, a persistent volume and a hardened container.
There is deliberately **one** service: no Tempo/Jaeger/Prometheus/Grafana (v1 decision). Prometheus can scrape
`/metrics` if you already run one.

## A. Run with Docker (needs Docker Desktop running)
```
copy .env.example .env          (Windows)   |   cp .env.example .env
:: edit .env: set API_TOKEN (recommended)
docker compose up --build -d
docker compose ps               :: STATUS should become "healthy" within ~30 s
python scripts/smoke_test.py http://127.0.0.1:8100 --token YOUR_API_TOKEN
:: open http://127.0.0.1:8100/dashboard   (paste the token when asked)
docker compose logs -f obsdash
docker compose down             :: keeps data;   docker compose down -v  deletes the data volume
```
Optional synthetic demo data (before or after `up`): `docker compose --profile demo run --rm seed`.
Alert rules / pricing: copy a file from `config/`, edit, set `ALERT_RULES_PATH=/config/<file>` or
`PRICING_TABLE_PATH=/config/<file>` in `.env`, then `docker compose up -d`. (The shipped examples have placeholder
thresholds and **fake** prices.)

Point your apps at it: Project 1 `.env` -> `OTEL_ENABLED=true`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8100/v1/traces`
(+ `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>` if you set one). If an app itself runs in a container use
`http://host.docker.internal:8100/v1/traces`.

## B. Run without Docker
```
python -m venv .venv && .venv\Scripts\activate        (Windows)   |  source .venv/bin/activate
pip install -r requirements.txt
set API_TOKEN=<optional>                               |  export API_TOKEN=...
python -m uvicorn obsdash.main:app --port 8100
python scripts/smoke_test.py http://127.0.0.1:8100
```
Default host is 127.0.0.1 (loopback). Data file: `obsdash.db` in the working directory (`SQLITE_PATH` to change).

## What is hardened and why
| Choice | Reason |
|---|---|
| Published as `127.0.0.1:8100` on the host | the telemetry is operationally sensitive; exposing it is an explicit edit |
| Non-root user (uid 10001), `cap_drop: ALL`, `no-new-privileges`, read-only root FS | a compromised process can't modify the image or escalate |
| Only `/data` (named volume) + `/tmp` writable | SQLite file persists; nothing else can be tampered with |
| No secrets in image or compose file; `.env` and `*.db` in `.dockerignore` | `docker history` / pushed images never contain them |
| Healthcheck uses `/healthz` (no token, not recorded as traffic) with stdlib only | no curl in the image, no token in the healthcheck |
| Container listens on 0.0.0.0 | required inside a container; obsdash logs `api_unauthenticated_on_non_loopback_host` when no token is set (expected with the host-loopback publish; set `API_TOKEN` to silence it) |

## Verification status (be precise about what was and was NOT run)
Verified in the sandbox:
- `docker compose config` and `--profile demo config`: valid.
- A clean virtualenv with **only** `requirements.txt` (no dev packages) and **only** the files the Dockerfile copies
  (`obsdash/`, `scripts/`, `config/`), started with the exact CMD and the container's env (`HOST=0.0.0.0`,
  `SQLITE_PATH`, empty-string env values as compose passes them): healthcheck command exit 0; the seed command works;
  `smoke_test.py` 6/6 without a token, 7/7 with a token (rules + example pricing loaded: 6 rules); a wrong token is rejected;
  a bad alert-rules file stops startup with a readable error (so the container fails fast instead of running broken).
- 6 pytest checks pin the security-relevant Docker lines and run the smoke script against a live server.

**NOT verified (no Docker daemon / no registry access in the sandbox):** `docker build`, `docker compose up`, the
actual healthy status, read-only-filesystem behaviour, named-volume ownership, `host.docker.internal`.
Please run section A and report: (1) `docker compose ps` output, (2) the smoke test output, (3) any build error text.
Likeliest failure points if something breaks: a Python base-image tag unavailable to you, or a port 8100 clash (`HOST_PORT=8101`).
