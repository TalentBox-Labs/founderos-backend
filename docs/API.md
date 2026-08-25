# Founder OS API Documentation

Complete reference for the Founder OS API, including endpoints, authentication, request/response formats, and usage examples.

## Quick Start

### 1. Authentication

All API requests require an API key in the Authorization header:

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://api.workcrew.ai/health
```

Get your API key from the Settings page in the web UI.

### 2. Base URL

- **Production**: `https://api.workcrew.ai`
- **Development**: `http://localhost:8000`

### 3. Response Format

All endpoints return JSON with a standard structure:

```json
{
  "ok": true,
  "message": "Operation completed successfully",
  "data": { ... }
}
```

Error responses include `ok: false` and a `detail` field:

```json
{
  "ok": false,
  "detail": "Invalid API key provided"
}
```

## Core Concepts

### Content Weeks

Content is organized by **weeks** identified by IDs like `W99`, `W01`, etc. Each week has:
- Tracker row: metadata (title, status, current step)
- Input artifacts: source content and briefs
- Output artifacts: generated content, final versions, reports

### Pipeline Stages

1. **Validation** — Check structure, metadata, content quality
2. **Generation** — Create briefs, SEO plans, research, drafts
3. **Editing** — Refine drafts into final publishable content
4. **Publishing** — Publish to canonical locations, distribute

### State Management

Weeks progress through states stored in `data/runtime_config.json`:
- `active_week` — Current week being processed
- `current_step` — Current stage in pipeline
- `status` — Publishing status (draft, review, published)

## API Sections

### 1. Pipeline Management

Execute content workflows: validation, generation, editing, publishing.

#### POST /run

Execute complete pipeline for a week.

**Request:**
```json
{
  "week": "W99",
  "topic": "AI Recruiting"
}
```

**Response:**
```json
{
  "ok": true,
  "week": "W99",
  "stdout": "Pipeline completed successfully",
  "steps": [
    {"status": "ok", "step": "validators", "message": "All checks passed"}
  ]
}
```

#### POST /validate

Run validators only (no generation).

**Request:**
```json
{
  "week": "W99"
}
```

**Response:**
```json
{
  "ok": true,
  "stdout": "All validators passed",
  "stderr": ""
}
```

#### POST /generate

Run Phase 2A generation crew (Strategist → SEO → Researcher → Writer).

**Request:**
```json
{
  "week": "W99"
}
```

**Response:**
```json
{
  "ok": true,
  "stdout": "Generation completed: 01_Brief, 02_SEO, 03_Research, 04_Draft"
}
```

#### POST /edit

Run Phase 2B editor crew to refine draft into final.

**Request:**
```json
{
  "week": "W99"
}
```

**Response:**
```json
{
  "ok": true,
  "stdout": "Final markdown generated from 04_Draft.md"
}
```

#### POST /switch-week

Switch active week and apply configuration profile.

**Request:**
```json
{
  "week": "W99"
}
```

**Response:**
```json
{
  "ok": true,
  "week": "W99",
  "steps": [
    {"status": "ok", "message": "Runtime config updated"}
  ]
}
```

### 2. Marketing Content Generation

Generate multi-channel marketing content (blog, social, email).

#### POST /marketing/generate

Generate marketing content for a topic across all channels.

**Request:**
```json
{
  "brand": "workcrew",
  "topic": "AI-Powered Recruiting",
  "keyword": "ai recruiting tools",
  "geo": "United States",
  "funnel": "consideration",
  "channel": "all"
}
```

**Response:**
```json
{
  "ok": true,
  "stdout": "Marketing content generated",
  "output_path": "output/marketing/workcrew/ai-recruiting-tools"
}
```

**Parameters:**
- `brand`: `workcrew` | `hirestack` | `founder`
- `funnel`: `awareness` | `consideration` | `decision`
- `channel`: `all` | `blog` | `social` | `email`

#### POST /marketing/dry-run

Preview what will be published without hitting external APIs.

**Request:**
```json
{
  "status_path": "output/marketing/workcrew/ai-recruiting-tools/08_Publish_Status.json"
}
```

**Response:**
```json
{
  "ok": true,
  "preview": {
    "blog": { "title": "...", "url": "..." },
    "linkedin": { "text": "...", "image": "..." },
    "instagram": { "caption": "...", "image_url": "..." }
  }
}
```

#### POST /marketing/publish

Publish to live channels (LinkedIn, Instagram, Hashnode, YouTube).

**Request:**
```json
{
  "status_path": "output/marketing/workcrew/ai-recruiting-tools/08_Publish_Status.json",
  "confirmed": true
}
```

**Response:**
```json
{
  "ok": true,
  "published": {
    "linkedin": { "url": "https://linkedin.com/feed/update/urn:li:..." },
    "instagram": { "post_id": "ABC123" },
    "hashnode": { "url": "https://hashnode.com/post/..." }
  }
}
```

### 3. Orchestration

Multi-agent orchestration for complex workflows.

#### POST /api/v1/orchestration/plan

Plan orchestration strategy.

**Request:**
```json
{
  "backend": "hermes",
  "brand": "workcrew",
  "topic": "AI Recruiting",
  "keyword": "ai recruiting tools",
  "geo_target": "United States",
  "funnel_stage": "consideration",
  "channels": ["blog", "linkedin", "email"]
}
```

**Response:**
```json
{
  "ok": true,
  "plan": {
    "brand": "workcrew",
    "playbooks": {
      "content_strategy": { "steps": [...] },
      "email_marketing": { "steps": [...] }
    }
  }
}
```

#### POST /api/v1/orchestration/run

Execute orchestration workflow.

**Request:**
```json
{
  "backend": "hermes",
  "brand": "workcrew",
  "topic": "AI Recruiting",
  "keyword": "ai recruiting tools",
  "run_content": true,
  "run_seo": true,
  "run_email": true
}
```

**Response:**
```json
{
  "ok": true,
  "result": {
    "run_id": "orch_20240615_abc123",
    "executions": {
      "seo_backend": { "status": "ok" },
      "email_backend": { "status": "ok" }
    }
  }
}
```

#### GET /api/v1/orchestration/logs?limit=20

Retrieve orchestration execution logs.

**Response:**
```json
{
  "ok": true,
  "runs": [
    {
      "run_id": "orch_20240615_abc123",
      "timestamp": "2024-06-15T10:30:00Z",
      "status": "completed"
    }
  ]
}
```

### 4. Outreach Management

Manage automated outreach sequences.

#### GET /api/v1/outreach/sequences

List active outreach sequences.

**Response:**
```json
{
  "ok": true,
  "sequences": [
    {
      "id": "seq_001",
      "name": "Welcome Series",
      "channel": "email",
      "steps_count": 5
    }
  ]
}
```

### 5. Health & Status

#### GET /health

Service health check (canonical liveness; use for probes / load balancers).

**Response:**
```json
{
  "status": "ok",
  "service": "WorkCrew CMS OS"
}
```

#### GET /health/debug

Operator diagnostics (project root, Python executable). Not for probes.

**Response:**
```json
{
  "status": "ok",
  "service": "WorkCrew CMS OS",
  "project_root": "/path/to/repo",
  "python": "/path/to/python"
}
```

#### GET /api/v1/health

API health check.

**Response:**
```json
{
  "status": "ok",
  "service": "WorkCrew CMS OS API"
}
```

#### POST /run-pipeline

Legacy pipeline alias (optional week apply, then `pipeline_orchestrator`).

**Response `status` values:** `"ok"` | `"failed"` (not `"success"`).

## Error Codes

| Code | HTTP | Meaning |
|------|------|---------|
| AUTH_INVALID_KEY | 401 | API key is invalid or missing |
| AUTH_EXPIRED | 401 | API key has expired |
| VALIDATION_ERROR | 400 | Request validation failed |
| WEEK_NOT_FOUND | 404 | Week ID doesn't exist |
| WEEK_INVALID_FORMAT | 400 | Week ID format invalid |
| OPERATION_TIMEOUT | 504 | Operation exceeded 10-minute timeout |
| INTERNAL_ERROR | 500 | Server-side error |

## Best Practices

### 1. Request Timeout Handling

Long-running operations (pipeline execution) have a 10-minute timeout:

```python
import requests
import time

response = requests.post(
    "https://api.workcrew.ai/run",
    json={"week": "W99"},
    headers={"Authorization": "Bearer API_KEY"},
    timeout=600  # 10 minutes
)

if response.status_code == 504:
    # Operation timed out, check status separately
    status = requests.get("/status/last-run")
```

### 2. Polling for Long Operations

For background operations, poll for completion:

```python
import time

response = requests.post(
    "https://api.workcrew.ai/run",
    json={"week": "W99"},
    headers={"Authorization": "Bearer API_KEY"}
)
run_id = response.json()["run_id"]

while True:
    status = requests.get(
        f"https://api.workcrew.ai/status/{run_id}",
        headers={"Authorization": "Bearer API_KEY"}
    )
    if status.json()["completed"]:
        break
    time.sleep(5)
```

### 3. Error Handling

Always check the `ok` field and handle errors:

```python
response = requests.post(
    "https://api.workcrew.ai/validate",
    json={"week": "W99"},
    headers={"Authorization": "Bearer API_KEY"}
)

if not response.json()["ok"]:
    error = response.json().get("detail", "Unknown error")
    print(f"Validation failed: {error}")
```

### 4. Idempotency

Operations like `/validate` and `/generate` are idempotent and can be safely retried:

```python
max_retries = 3
for attempt in range(max_retries):
    try:
        response = requests.post(...)
        if response.ok:
            break
    except Exception as e:
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # Exponential backoff
```

## Rate Limiting

API endpoints are rate-limited:

| Endpoint | Limit |
|----------|-------|
| `/run` | 1 per hour per week |
| `/validate` | 5 per hour per week |
| `/generate` | 2 per hour per week |
| `/marketing/generate` | 5 per hour |
| Other endpoints | 60 per minute |

Rate limit headers in responses:
- `X-RateLimit-Limit`: Maximum requests in window
- `X-RateLimit-Remaining`: Requests remaining
- `X-RateLimit-Reset`: Unix timestamp when limit resets

## Code Examples

### Python with Requests

```python
import requests

API_KEY = "sk_live_abc123xyz"
BASE_URL = "https://api.workcrew.ai"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Run full pipeline
response = requests.post(
    f"{BASE_URL}/run",
    json={"week": "W99"},
    headers=headers,
    timeout=600
)

result = response.json()
print(f"Status: {'✓' if result['ok'] else '✗'}")
print(f"Output: {result.get('stdout', '')}")
```

### JavaScript with Fetch

```javascript
const API_KEY = "sk_live_abc123xyz";
const BASE_URL = "https://api.workcrew.ai";

async function runPipeline(week) {
  const response = await fetch(`${BASE_URL}/run`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ week })
  });

  const result = await response.json();
  console.log(result.ok ? "✓ Success" : "✗ Failed");
  return result;
}
```

### cURL

```bash
curl -X POST https://api.workcrew.ai/run \
  -H "Authorization: Bearer sk_live_abc123xyz" \
  -H "Content-Type: application/json" \
  -d '{"week":"W99"}' \
  --max-time 600
```

## Interactive Documentation

- **Swagger UI**: `https://api.workcrew.ai/docs` (try-it-out interface)
- **ReDoc**: `https://api.workcrew.ai/redoc` (detailed reference)

## Support

For API support, contact: `api-support@workcrew.ai`

## Changelog

### Version 2.0.0 (2024-06-15)
- Modularized router architecture
- BaseCrew pattern for all crews
- Enhanced OpenAPI documentation
- Improved error handling

### Version 1.0.0 (2024-01-01)
- Initial public API release
