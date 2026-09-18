# ZERO-COST BASIC staging — redacted operator checklist

This checklist is documentation only. It does **not** authorize secret generation,
Neon URL retrieval, GitHub permission changes, Render service creation, deploy,
OAuth changes, payment methods, or paid SKUs.

## Scope

- **FOUNDER OS BASIC / STAGING ONLY**
- **NOT production-equivalent** (no production SLA, no always-on heartbeat, Free spin-down/cold starts)
- **EXPECTED_INCREMENTAL_HOSTING_CHARGE = $0**

## Frozen architecture

- Existing Render **Hobby** workspace only (do not create another account/workspace to evade quotas)
- Future: **ONE** Render Free Web Service (Ohio if Free available; colocate with Neon Ohio)
- Existing Neon Free project only: `founderos-staging` (AWS US East 2 / Ohio) — **no new Neon project**
- Public host: default `*.onrender.com` HTTPS only — **no custom domain**
- Database: external Neon Free PostgreSQL only — **no Render PostgreSQL**, **no Render Key Value**
- **No** persistent disk, worker, cron, or workflow
- Auto-Deploy: **Off**
- **Blueprint deployment is PROHIBITED** — do **not** apply `render.yaml` via Render Blueprint in this runbook (even though the file is Free-only as a footgun mitigation)

## Required environment (must exist BEFORE Create Web Service)

Render Create Web Service **triggers the first build and deploy**. Required startup
environment must be configured in Advanced before clicking Create.

| Variable | Required staging value |
|---|---|
| `HEARTBEAT_ENABLED` | `0` |
| `N8N_BRIDGE_ENABLED` | `0` |
| `FOUNDER_OS_COOKIE_SECURE` | `true` |
| `FOUNDER_OS_REQUIRE_LOGIN` | `true` |
| `SECRET_KEY` | `<REDACTED_STAGING_SECRET>` — **new**, ≥32 chars, never reuse production |
| `DATABASE_URL` | `<NEON_DIRECT_DATABASE_URL_WITH_SSLMODE_REQUIRE>` — private; never commit/chat |
| `FOUNDER_OS_BOOTSTRAP_EMAIL` | staging-only operator email |
| `FOUNDER_OS_BOOTSTRAP_PASSWORD` | `<REDACTED_STAGING_BOOTSTRAP_PASSWORD>` (≥12 chars, not a default) |
| `FOUNDER_OS_BOOTSTRAP_NAME` | valid human display name |
| `FOUNDER_OS_BOOTSTRAP_ORG_NAME` | staging org label |

## Runtime reminders

- Process binds `0.0.0.0` and `${PORT:-8000}`
- SQLAlchemy pool ceiling: `pool_size(10)+max_overflow(20)=30`
- `GET /health` = process **liveness only** (`database=not_checked`)
- `GET /health/ready` = database readiness (`SELECT 1`)
- Successful `/health` does **NOT** mean Founder OS staging acceptance
- Neon `DATABASE_URL` remains private; production secrets/data/Gmail credentials prohibited
- Google OAuth configuration occurs **only later**, after a stable Render hostname exists, under **separate** authorization

## Quota / billing fail-closed

If Free quotas are exhausted (instance hours, bandwidth, pipeline minutes, Neon CU/storage/egress):

- **STOP / WAIT / SUSPEND**
- **Never** upgrade, add a payment method, or move to a paid SKU to restore service under this contract
- **Never** add a payment method as part of this runbook

## Prohibited

- Payment methods, paid SKUs, paid trials, automatic overage paths
- Render Postgres / Key Value / disks / workers / cron / workflows / custom domains
- Committing secrets or Neon URLs
- Touching backend PR **#20** or PR **#23**
- Quota/account/workspace/service rotation to evade Free limits
