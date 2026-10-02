# ── Stage 1: build the React CRM frontend ────────────────────────────────────
# frontend/ is a git submodule (github.com/TalentBox-Labs/founderos-frontend) —
# the build context must have it checked out: `git submodule update --init`.
FROM node:20-slim AS frontend-build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-api.txt requirements-revenue.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-api.txt -r requirements-revenue.txt

COPY . .
COPY --from=frontend-build /build/dist ./frontend/dist

EXPOSE 8000

# Shell-form HEALTHCHECK so ${PORT} expands at probe time (Render sets PORT).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1

# Bind only after the ledger guard admits this image. PORT expands in the script.
CMD ["sh", "scripts/publication_admission_start.sh"]
