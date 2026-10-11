# FounderOs

For the current repositories, local setup, CI and application release procedure, see [Development and release baseline](docs/operations/DEVELOPMENT_AND_RELEASE.md).

A content management and scheduling system for technical hiring content automation.

## Quick Start

### Prerequisites
- Python 3.11+
- Poetry or pip

### Setup

1. **Clone and install dependencies:**
   ```bash
   git clone <repo>
   cd "S&M - CSM OS"
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Install pre-commit hooks** (optional but recommended):
   ```bash
   pip install pre-commit
   pre-commit install
   ```

3. **Generate/sync week fixtures:**
   ```bash
   python scripts/generate_week_fixtures.py
   ```

4. **Run tests:**
   ```bash
   python -m pytest tests/ -v
   ```

## Week Fixtures

Week fixtures include:
- **Profile JSON** — runtime config per week (`data/week_runtime/{WEEK}.json`)
- **Content markdown** — SEO plan, research, draft, final, checklist (`input/{WEEK}/*.md`)

### Fixture Management

All fixtures are generated from `tracker.csv` and a single template to keep everything in sync.

#### Regenerate fixtures
```bash
# Fill missing fixtures (preserves existing files)
python scripts/generate_week_fixtures.py

# Force regenerate all (overwrites everything)
python scripts/generate_week_fixtures.py --overwrite

# Verify all fixtures exist without writing
python scripts/generate_week_fixtures.py --verify
```

#### Why this matters
- **Tests expect fixtures** — validators (research_mapper, draft_validator, etc.) read from `input/{WEEK}/*.md`
- **Profiles point to content** — `data/week_runtime/{WEEK}.json` tells tools where to find each week's files
- **Tracker is source of truth** — new weeks added to `tracker.csv` automatically get fixture scaffolds

#### Common issues

**"Runtime config not found"**
- Ensure `data/runtime_config.json` exists
- Run: `python scripts/generate_week_fixtures.py`

**Tests fail with missing files**
- Check that `input/{WEEK}/` directories exist with all required markdown files
- Run: `python scripts/generate_week_fixtures.py`

**Validator tests fail but fixtures exist**
- Verify fixtures have required content (H1 headings, FAQ sections, front matter keys)
- See [tests/test_validator_steps_integration.py](tests/test_validator_steps_integration.py) for specifics

## Development

### Running tests
```bash
# All tests
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_crew.py -v

# With coverage
python -m pytest tests/ --cov=src --cov-report=term-missing
```

### Running validators manually
Each validator can be run independently:
```bash
python -m src.tools.draft_validator
python -m src.tools.research_mapper
python -m src.tools.structure_checker
python -m src.tools.metadata_checker
python -m src.tools.publish_checklist_checker
```

Validators read the active week from `WORKCREW_RUNTIME_CONFIG` env var (defaults to `data/runtime_config.json`).

### Environment Variables

**CrewAI & LLM:**
- `WORKCREW_CREWAI_MODEL` — model ID with optional provider prefix (default: `ollama/llama3.1:8b`)
- `WORKCREW_OLLAMA_BASE_URL` — Ollama server URL (default: `http://localhost:11434`)
- `WORKCREW_CREWAI_TEMPERATURE` — LLM temperature (default: `0`)
- `WORKCREW_CREWAI_API_KEY` — LLM API key if needed (default: `ollama`)

**Fixture Override:**
- `WORKCREW_RUNTIME_CONFIG` — path to override runtime config JSON (for staging/promotion dry-runs)

## CI/CD

GitHub Actions workflow runs on push/PR:
- Runs pytest suite against Python 3.11, 3.12, 3.13
- Verifies all fixtures are in sync
- Fails if fixtures are missing or tests don't pass

Run locally before pushing:
```bash
python -m pytest tests/ -q
python scripts/generate_week_fixtures.py --verify
```

Optional local safety check for accidental secrets:
```bash
grep -RInE --exclude-dir=.venv --exclude-dir=.git --exclude-dir=htmlcov --exclude=.env --exclude=.env.example '(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY)[[:space:]]*[:=][[:space:]]*[^[:space:]]+' .
```

## Architecture

```
src/
  ├── crew.py           # CrewAI LLM wiring + QA agent orchestration
  ├── agents.yaml       # CrewAI agent configs
  ├── tasks.yaml        # CrewAI task configs
  └── tools/            # Validators & utilities
      ├── draft_validator.py      # Draft content gate
      ├── research_mapper.py       # Research/SEO research gate
      ├── structure_checker.py     # Final markdown structure gate
      ├── metadata_checker.py      # YAML front matter validation
      └── ...

tests/                  # Unit & integration tests
data/
  ├── runtime_config.json          # Active week config (auto-generated)
  └── week_runtime/                # Week-specific profiles
input/                  # Content markdown by week
  ├── W01/              # Week 1 files (02_SEO_Plan.md, 04_Draft.md, etc.)
  └── ...
```

## Full Platform (FastAPI backend, `runner_api.py`)

This repository also contains the `revenue_os/` FastAPI application (`runner_api.py`),
the Sales/Marketing CRM API, Jinja-rendered founder/operator pages, and the
Celery-based background workers — not just the `src/` content pipeline
described above.

### Frontend dependency

The React CRM frontend lives in a separate repo,
[`founderos-frontend`](https://github.com/TalentBox-Labs/founderos-frontend),
checked out here as a git submodule at `frontend/`:

```bash
git submodule update --init --recursive
```

The production Docker build (`Dockerfile`, stage 1) builds this submodule
exactly as it built `frontend/` before the repository separation — no
Dockerfile logic changed, only where `frontend/` content comes from.

### Install (full platform)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-api.txt -r requirements-revenue.txt
```

### Database

Postgres 14 in production/CI; SQLite is used automatically as a local
fallback when `DATABASE_URL` is unset. `SECRET_KEY` is required — the app
raises a fatal error at startup without it.

```bash
docker compose up db redis   # Postgres + Redis, matching docker-compose.yml
```

Schema is created via `Base.metadata.create_all()` on startup, plus a
hand-written `_migrate_missing_columns()` helper for adding columns to
existing tables — Alembic (`alembic.ini`, `migrations/`) is present but
not the primary migration path today.

### Run the API

```bash
uvicorn runner_api:app --reload --host 0.0.0.0 --port 8000
```

### Background jobs

Two systems: an in-process heartbeat scheduler (starts automatically with
the API unless `HEARTBEAT_ENABLED=0`), and Celery workers:

```bash
celery -A revenue_os.tasks.celery_app worker --loglevel=info
celery -A revenue_os.tasks.celery_app beat --loglevel=info
```

### Production deployment

`docker-compose.yml` runs `db` (Postgres), `redis`, `api`, `worker`, and
`beat` from the same Docker image. `render.yaml` deploys the same
Dockerfile as a single Render web service plus a managed Postgres database.

## Contributing

1. Create a branch
2. Make changes (add fixtures, update validators, etc.)
3. Run local tests & fixture verify
4. Pre-commit hooks will run automatically on commit
5. Push and create PR

The CI pipeline will gate the PR on test pass + fixture verification.

## License

Internal. See LICENSE for details.
