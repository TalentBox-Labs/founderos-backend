# Deployment Guide — Founder OS

One Docker image serves everything: the FastAPI backend, the autonomous
heartbeat, and the React CRM UI (mounted at `/app`).

## What a deployed instance looks like

| URL | What |
|---|---|
| `https://<host>/app` | The CRM UI (login with the team API key) |
| `https://<host>/docs` | Swagger API explorer |
| `https://<host>/health` | Health check |
| `https://<host>/webhooks/n8n/{event}` | n8n inbound webhooks |

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | **yes** | App secret. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `RUNNER_API_KEY` | **yes (production)** | Team API key — protects every endpoint and is what users type on the CRM login screen. Without it the API is open (dev mode). |
| `DATABASE_URL` | yes | `postgresql://...` in production; `sqlite:///./demo.db` for testing |
| `OPENAI_API_KEY` | optional | Enables LLM features (CrewAI content crews) |
| `HEARTBEAT_ENABLED` | optional | `1` (default) runs autonomous jobs; `0` disables |
| `N8N_WEBHOOK_BASE_URL` | optional | Your n8n instance webhook base, e.g. `https://n8n.example.com/webhook` |
| `N8N_INBOUND_SECRET` | optional | Shared secret n8n sends as `X-N8N-Secret` when calling back |
| `CORS_ALLOWED_ORIGINS` | optional | Comma-separated origins if the UI is hosted separately |

## Option A — Render (easiest, ~10 minutes)

1. Push this repository to GitHub.
2. In [Render](https://render.com): **New → Blueprint**, select the repo.
   `render.yaml` provisions the web service + PostgreSQL automatically,
   generating `SECRET_KEY` and `RUNNER_API_KEY` for you.
3. When it's live, copy the `RUNNER_API_KEY` value from the service's
   Environment tab — that's what your team enters on the login screen at
   `https://<your-app>.onrender.com/app`.

## Option B — Railway

1. **New Project → Deploy from GitHub repo** (Railway detects the Dockerfile).
2. Add a PostgreSQL plugin; Railway injects `DATABASE_URL`.
3. Set `SECRET_KEY` and `RUNNER_API_KEY` in Variables.
4. Open `https://<app>.up.railway.app/app`.

## Option C — Any VPS with Docker

```bash
git clone <repo> && cd <repo>
docker build -t workcrew-crm .
docker run -d --name crm -p 8000:8000 \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -e RUNNER_API_KEY="pick-a-strong-team-key" \
  -e DATABASE_URL="postgresql://user:pass@dbhost:5432/crm" \
  workcrew-crm
```

Put nginx/Caddy with TLS in front for a public deployment.

## Local development (no Docker)

```bash
# Terminal 1 — backend
pip install -r requirements.txt -r requirements-api.txt
export SECRET_KEY=dev-secret-key-123456789012345678901234
export DATABASE_URL=sqlite:///./demo.db
python -m uvicorn runner_api:app --port 8000

# Terminal 2 — frontend (hot reload)
cd frontend && npm install && npm run dev
# open http://localhost:5173 — leave the API key blank on login (dev mode)
```

## Database notes

- Tables are created automatically on startup (`Base.metadata.create_all`)
  — a fresh PostgreSQL or SQLite database needs no manual migration.
- SQLite is fine for demos/testing; use PostgreSQL for production.

## Connecting n8n

1. Deploy n8n (n8n.cloud or self-hosted).
2. Set `N8N_WEBHOOK_BASE_URL` on this app to your n8n webhook base URL.
3. In n8n, create Webhook-triggered workflows named after the outbound
   catalog (`revenue-os-events`, `send-email`, `new-lead`, ...).
4. Have n8n workflows report results back:
   `POST https://<host>/webhooks/n8n/{event}` with header
   `X-N8N-Secret: <N8N_INBOUND_SECRET>`.
5. `GET /webhooks/n8n/catalog` documents both directions.

## Security checklist before inviting users

- [ ] `RUNNER_API_KEY` set to a strong value (it gates everything)
- [ ] `SECRET_KEY` unique per environment
- [ ] HTTPS in front (Render/Railway provide it automatically)
- [ ] PostgreSQL with backups (managed database recommended)
- [ ] Review `/api/v1/heartbeat/activity` periodically — it's the audit
      trail of everything the autonomous side does
