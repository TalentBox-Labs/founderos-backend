# Development and release baseline

## Canonical repositories

- Backend: `https://github.com/TalentBox-Labs/founderos-backend.git`, branch `Main`.
- Frontend: `https://github.com/TalentBox-Labs/founderos-frontend.git`, branch `Main`.

`FounderOS-Sales_Marketing` is the previous GitHub name of the backend and redirects to the same repository. Existing local copies are separate checkouts, not separate products. Use `git remote set-url origin https://github.com/TalentBox-Labs/founderos-backend.git` in a checkout still using the previous URL. Do not reset an old local `Main` branch to overwrite unique commits. Preserve local branches and uncommitted work before changing checkouts.

The organized laptop workspace is `Documents/FounderOS`: repositories, worktrees, reference, and backups. Repository commands below use relative paths, so folder moves do not affect them.

## Start from a clean reviewed baseline

```sh
git clone --recurse-submodules https://github.com/TalentBox-Labs/founderos-backend.git
cd founderos-backend
git switch -c your-development-branch
python3 scripts/setup_local_env.py
docker compose up --build -d
```

Use Docker Compose v2. The setup helper creates private local credentials and preserves any existing `.env`. The local database enables TLS, matching the runtime's fail-closed PostgreSQL policy. Ports bind to localhost. The new TLS database volume is `founderos_dev_tls_pgdata`; previous local volumes are retained but are not automatically migrated. Export/import any needed old development data before retiring those volumes.

API: `http://127.0.0.1:8000/health`; bundled React UI: `http://127.0.0.1:8000/app/`; main operating UI: `http://127.0.0.1:8000/`. Login is required. Set the documented `FOUNDER_OS_BOOTSTRAP_*` values separately for a real human account; never reuse production credentials locally. For a demo use both Compose files: `docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build -d`.

Background workers are optional: `docker compose --profile background up -d`. Heartbeat and external bridges remain disabled by default. Enabling autonomous or external integrations requires their explicit tenant and credential configuration.

For frontend hot reload: `cd frontend`, use Node.js 24, then `npm ci` and `npm run dev`. Vite proxies `/api` to the local backend. The pinned submodule is detached intentionally; make frontend changes in the standalone frontend repository, merge them there, then update the backend pointer through a reviewed pull request. Do not use `git submodule update --remote` for production: release the exact reviewed commit.

## Validation

For Python development, create a virtual environment with Python 3.11, 3.12 or 3.13 and install all three requirements files. Run `python -m pytest tests -q` and `python scripts/generate_week_fixtures.py --verify` in a clean checkout without production environment variables. Tests provision a separate SQLite database.

GitHub pull requests run the Python version matrix, tests/build/audit for the exact pinned frontend, and a combined Docker build with a local TLS database startup check. Frontend pull requests independently run tests, build, and audit. A passing source test alone does not certify the image.

## Staging and production

The existing Render configuration disables automatic deployment. This local Compose setup is for development and CI only; do not publish its database/Redis ports, reuse its credentials in production, or use its self-signed certificate as production infrastructure.

Before a manual release, require successful checks for the exact backend commit and pinned frontend commit. Validate that target-specific secrets, TLS database URL, secure cookies, tenant membership and human login are configured. Back up the target database and verify the publication-ledger admission step. Smoke-test `/health`, login, the main UI and `/app` in staging. Validate a rollback image and its database compatibility before promotion. Record the backend SHA, frontend SHA, environment and rollback SHA in the release record.

The older `DEPLOYMENT_RUNBOOK.md` covers the Cloudflare content website pipeline. It is not the full application deployment procedure. Consult the application staging documentation and `render.yaml` for the application-specific settings. No folder cleanup or repository housekeeping operation deploys the application.

Repository administrators should require pull requests and the Python `test` jobs, `frontend`, and `container` checks on `Main`, and prohibit force pushes/deletion. Push permission alone cannot configure these GitHub controls. Old conflicting checkpoint pull requests must be reviewed for intent and superseded or reconciled before merging; they are not the release baseline.
