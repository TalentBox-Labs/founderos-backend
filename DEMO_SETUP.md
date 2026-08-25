# Founder OS - Demo/Testing Setup

Complete guide for running the platform locally for internal testing before production deployment.

## Quick Start (5 minutes)

### Prerequisites
- Docker & Docker Compose installed
- Git repository cloned
- Port 8000, 5432, 6379 available

### One-Command Setup

```bash
# Clone environment and start
cp .env.example .env
docker-compose -f docker-compose.demo.yml up --build

# In another terminal, initialize database
docker-compose -f docker-compose.demo.yml exec api python scripts/init_demo_db.py

# Access platform
# API: http://localhost:8000
# Docs: http://localhost:8000/docs
# ReDoc: http://localhost:8000/redoc
```

## Detailed Setup Guide

### 1. Environment Configuration

```bash
# Copy template
cp .env.example .env

# Edit .env with demo values (optional - defaults work)
nano .env
```

**Minimum required for demo:**
```env
SECRET_KEY=demo-key-change-in-production-12345678901234567890
OPENAI_API_KEY=sk-demo-key  # or add real key for LLM features
DATABASE_URL=postgresql://demo_user:demo_pass@db:5432/revenue_os_demo
REDIS_URL=redis://redis:6379/0
```

### 2. Docker Compose Setup

The platform includes three Docker configurations:

**Option A: Full Stack (Recommended for testing)**
```bash
docker-compose -f docker-compose.demo.yml up --build
```
- PostgreSQL (demo database)
- Redis (caching & queues)
- API Server (FastAPI)
- Celery Worker (background jobs)
- Celery Beat (scheduled tasks)

**Option B: API Only (Quick testing)**
```bash
docker-compose -f docker-compose.yml up db redis api
```

**Option C: Local Development (No Docker)**
```bash
# Install dependencies
pip install -r requirements.txt -r requirements-api.txt -r requirements-revenue.txt

# Start PostgreSQL locally or use sqlite
export DATABASE_URL=sqlite:///./demo.db
export SECRET_KEY=demo-key-12345678901234567890123456789012
export REDIS_URL=redis://localhost:6379/0

# Run API
uvicorn runner_api:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Database Initialization

Two options for demo data:

**Option A: Automated Demo Data (Recommended)**
```bash
# Create demo data with sample metrics, dashboards, contacts, deals
docker-compose -f docker-compose.demo.yml exec api python scripts/init_demo_db.py

# Output: ✅ Demo database initialized with:
#   - 10 sample contacts
#   - 5 active deals
#   - 3 email sequences
#   - 5 pre-built dashboards
#   - 100+ sample data points
```

**Option B: Manual Setup**
```bash
# Run migrations
docker-compose -f docker-compose.demo.yml exec api alembic upgrade head

# Database ready for manual testing
```

## Testing the Platform

### 1. API Health Check

```bash
curl -X GET http://localhost:8000/health
# Expected: {"status": "ok", "service": "WorkCrew CMS OS"}
```

### 2. Interactive API Documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI Schema**: http://localhost:8000/openapi.json

### 3. Test Key Endpoints

**Phase 13 - Analytics & Marketing**
```bash
# Create a metric
curl -X POST http://localhost:8000/analytics/metrics \
  -H "Authorization: Bearer demo-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Demo Revenue",
    "metric_type": "revenue",
    "calculation": "SUM(deals.value)",
    "unit": "$",
    "description": "Total demo revenue"
  }'

# List all dashboards
curl http://localhost:8000/analytics/dashboards \
  -H "Authorization: Bearer demo-api-key"

# Get dashboard templates
curl http://localhost:8000/analytics/dashboards/templates \
  -H "Authorization: Bearer demo-api-key"
```

**Phase 12 - WhatsApp**
```bash
# List WhatsApp contacts
curl http://localhost:8000/api/v1/whatsapp/contacts \
  -H "Authorization: Bearer demo-api-key"

# Send test message
curl -X POST http://localhost:8000/api/v1/whatsapp/send \
  -H "Authorization: Bearer demo-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+1234567890",
    "message": "Test message"
  }'
```

### 4. Test Scenarios

**Scenario 1: Marketing Automation Flow**
1. Create email sequence via `/marketing/email-sequences`
2. Create lead via `/leads`
3. Subscribe lead to sequence
4. Verify lead scoring via `/marketing/lead-score/{lead_id}`
5. Check email delivery status

**Scenario 2: Analytics Dashboard**
1. Create metrics via `/analytics/metrics`
2. Record data points via `/analytics/data-points`
3. Create dashboard via `/analytics/dashboards`
4. Add widgets to dashboard via `/analytics/dashboards/{id}/widgets`
5. Query dashboard data via `/analytics/dashboards/{id}`

**Scenario 3: Sales Pipeline**
1. Import contacts via `/contacts`
2. Create deals via `/deals`
3. View pipeline status via `/deals?stage=qualification`
4. Run forecasting via `/forecasting/forecast`
5. Check at-risk accounts via `/csm/at-risk-accounts`

## Monitoring & Debugging

### View Logs

```bash
# All services
docker-compose -f docker-compose.demo.yml logs -f

# Specific service
docker-compose -f docker-compose.demo.yml logs -f api
docker-compose -f docker-compose.demo.yml logs -f worker
```

### Database Access

```bash
# PostgreSQL CLI
docker-compose -f docker-compose.demo.yml exec db psql -U demo_user -d revenue_os_demo

# Redis CLI
docker-compose -f docker-compose.demo.yml exec redis redis-cli
```

### Performance Testing

```bash
# Load test (requires Apache Bench)
ab -n 1000 -c 10 http://localhost:8000/health

# Or use wrk
wrk -t4 -c100 -d30s http://localhost:8000/health
```

## Troubleshooting

### Port Already in Use
```bash
# Find and kill process using port 8000
lsof -i :8000
kill -9 <PID>

# Or use different port
docker-compose -f docker-compose.demo.yml up -p 8001:8000
```

### Database Connection Error
```bash
# Check PostgreSQL is running
docker-compose -f docker-compose.demo.yml ps db

# Restart database
docker-compose -f docker-compose.demo.yml restart db

# Check logs
docker-compose -f docker-compose.demo.yml logs db
```

### Redis Connection Error
```bash
# Verify Redis is accessible
docker-compose -f docker-compose.demo.yml exec redis redis-cli ping
# Expected: PONG

# If fails, restart
docker-compose -f docker-compose.demo.yml restart redis
```

### Module Import Errors
```bash
# Rebuild containers with fresh dependencies
docker-compose -f docker-compose.demo.yml build --no-cache api

# Restart
docker-compose -f docker-compose.demo.yml up
```

## Cleanup

### Stop All Services
```bash
docker-compose -f docker-compose.demo.yml down
```

### Remove Demo Data (Keep DB Schema)
```bash
docker-compose -f docker-compose.demo.yml exec api python scripts/clear_demo_data.py
```

### Full Reset (Delete All Data)
```bash
docker-compose -f docker-compose.demo.yml down -v
```

## Next Steps: Moving to Production

Once testing is complete:

1. **Update Environment**
   - Use production secrets in .env
   - Configure real LLM API keys
   - Set up production database (managed service)

2. **Deploy**
   ```bash
   # Use production docker-compose
   docker-compose up -d
   
   # Or use your hosting platform (AWS ECS, Render, Railway, etc.)
   ```

3. **Database Backup**
   ```bash
   docker-compose exec db pg_dump -U demo_user revenue_os_demo > backup.sql
   ```

4. **Security Checklist**
   - [ ] Generate new SECRET_KEY
   - [ ] Add real API keys (OpenAI, etc.)
   - [ ] Configure SSL/TLS
   - [ ] Set up backup strategy
   - [ ] Configure monitoring & alerting
   - [ ] Enable rate limiting
   - [ ] Set up authentication & authorization

## Support

For issues or questions:
- Check logs: `docker-compose -f docker-compose.demo.yml logs -f api`
- Review error messages in API responses
- Check database migrations: `alembic current`
- Verify network connectivity between services

## Architecture Overview

```
┌─────────────────────────────────────────────┐
│       Client / Testing Tools                │
│   (cURL, Postman, Browser, Python)          │
└────────────────┬────────────────────────────┘
                 │
        ┌────────▼────────┐
        │   Docker Network│
        └────────┬────────┘
         ┌──────┼──────────────────┐
         │      │                  │
    ┌────▼─┐ ┌─▼─────┐ ┌─────────┴──┐
    │ API  │ │  DB   │ │   Redis    │
    │ 8000 │ │ 5432  │ │   6379     │
    └────┬─┘ └───────┘ └────────────┘
         │
    ┌────▼──────┬─────────┐
    │            │         │
 ┌──▼──┐  ┌──────▼──┐ ┌───▼───┐
 │Worker│  │  Beat   │ │ Utils │
 │Jobs  │  │Scheduler│ │Lib    │
 └──────┘  └─────────┘ └───────┘
```

## Key Files

- `runner_api.py` - FastAPI application entry point
- `docker-compose.demo.yml` - Demo environment configuration
- `scripts/init_demo_db.py` - Demo data initialization
- `scripts/clear_demo_data.py` - Clean demo data
- `revenue_os/` - Core platform modules
- `runner_api_routers/` - API endpoints

## Documentation Links

- [API Documentation](http://localhost:8000/docs)
- [Phase 12 - WhatsApp Ecosystem](docs/WHATSAPP_ECOSYSTEM.md)
- [Phase 13 - Analytics & Marketing](docs/MARKETING_STRATEGY_AEO_GEO.md)
- [Sales Collateral Library](docs/SALES_COLLATERAL_LIBRARY.md)
