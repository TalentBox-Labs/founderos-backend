# Founder OS Observability Guide

Comprehensive guide to the observability stack covering structured tracing, metrics collection, and health monitoring.

## Overview

The observability system provides complete visibility into system behavior:

- **Structured Tracing** — Correlation IDs track requests across services
- **Metrics Collection** — Performance metrics (latency, throughput, errors)
- **Crew Execution Metrics** — AI crew performance tracking
- **Health Monitoring** — Component status and availability
- **Custom Instrumentation** — Application-level metrics

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Request                                                     │
├─────────────────────────────────────────────────────────────┤
│ RequestTracer (Correlation ID)                              │
├─────────────────────────────────────────────────────────────┤
│ Endpoint Logic                                              │
│ - MetricsCollector.timer() for duration                     │
│ - MetricsCollector.record() for values                      │
│ - CrewMetricsCollector for AI crew tracking                │
├─────────────────────────────────────────────────────────────┤
│ Metrics Export                                              │
│ - JSON API: /api/v1/metrics                                 │
│ - Prometheus: /api/v1/metrics/prometheus                    │
│ - Health: /api/v1/system/health                             │
│ - Summary: /api/v1/status/summary                           │
└─────────────────────────────────────────────────────────────┘
```

## Metrics System

### Metric Types

1. **Counter** — Monotonically increasing values
   ```python
   MetricsCollector.increment("requests_total", labels={"endpoint": "/run"})
   ```

2. **Gauge** — Point-in-time values
   ```python
   MetricsCollector.set_gauge("active_connections", 15)
   ```

3. **Histogram** — Distribution of values
   ```python
   MetricsCollector.record("request_latency_ms", 125.5)
   ```

4. **Timer** — Automatic duration tracking
   ```python
   with MetricsCollector.timer("operation_duration_ms"):
       perform_operation()
   ```

### Built-in Metrics

#### Request Metrics
- `requests_total` — Total requests processed
- `requests_failed` — Failed requests
- `request_latency_ms` — Request duration distribution

#### Crew Metrics
- `crew_executions_total` — Total crew executions by crew type
- `crew_duration_ms` — Crew execution duration distribution
- `crew_validations_passed` — Passed validations by crew
- `crew_validations_failed` — Failed validations by crew

#### System Metrics
- `active_connections` — Current database connections
- `cache_hits` — Cache hit count
- `cache_misses` — Cache miss count

### Accessing Metrics

#### JSON Format
```bash
curl -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/metrics
```

Response:
```json
{
  "timestamp": "2024-06-15T10:30:00.123456",
  "uptime_seconds": 86400,
  "counters": {
    "requests_total": 15420,
    "requests_failed": 23
  },
  "gauges": {
    "active_connections": 8
  },
  "histograms": {
    "request_latency_ms": {
      "count": 15420,
      "sum": 1927500,
      "mean": 125.1,
      "min": 12.5,
      "max": 8950.3,
      "p50": 95.2,
      "p95": 450.8,
      "p99": 2150.3
    }
  }
}
```

#### Prometheus Format
```bash
curl -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/metrics/prometheus
```

Response:
```
requests_total 15420
requests_failed_total 23
active_connections 8
request_latency_ms_count 15420
request_latency_ms_sum 1927500
request_latency_ms_mean 125.1
request_latency_ms_p95 450.8
request_latency_ms_p99 2150.3
```

## Request Tracing

### Correlation IDs

Every request gets a unique correlation ID for tracking:

```python
request_id = RequestTracer.start_request()
# All logs for this request include the ID
# request_id example: req_abc123def456
```

### Request Context

Store arbitrary context data with a request:

```python
RequestTracer.set_context("week_id", "W99")
RequestTracer.set_context("user_id", "user_123")

context = RequestTracer.get_context()
# {"week_id": "W99", "user_id": "user_123"}
```

### Duration Tracking

Get elapsed time for current request:

```python
duration_ms = RequestTracer.get_duration_ms()
# Returns milliseconds since request start
```

### Trace Endpoints

#### GET /api/v1/trace/info

Get current request tracing information:

```json
{
  "ok": true,
  "request_id": "req_abc123def456",
  "duration_ms": 250.5,
  "context": {
    "week_id": "W99",
    "user_id": "user_123"
  }
}
```

## Crew Execution Metrics

### Tracking Crew Runs

Record crew execution metrics:

```python
from src.observability import CrewMetricsCollector, CrewMetrics

metrics = CrewMetrics(
    crew_name="generation",
    week_id="W99",
    task_count=4,
    agent_count=4,
    total_duration_ms=12500.5,
    validation_passed=True,
    token_usage_estimate=8500
)

CrewMetricsCollector.record_execution(metrics)
```

### Crew Execution Endpoints

#### GET /api/v1/crew/executions

Get crew execution history:

```bash
# All crews
curl -H "Authorization: Bearer API_KEY" \
  "https://api.workcrew.ai/api/v1/crew/executions?limit=50"

# Specific crew
curl -H "Authorization: Bearer API_KEY" \
  "https://api.workcrew.ai/api/v1/crew/executions?crew=generation&limit=20"
```

Response:
```json
{
  "ok": true,
  "crew": "generation",
  "summary": {
    "executions": 42,
    "passed": 39,
    "failed": 3,
    "pass_rate": 0.929,
    "avg_duration_ms": 12750.3,
    "min_duration_ms": 8900.2,
    "max_duration_ms": 24150.7
  },
  "executions": [
    {
      "crew_name": "generation",
      "week_id": "W99",
      "task_count": 4,
      "agent_count": 4,
      "total_duration_ms": 12750.3,
      "validation_passed": true,
      "validation_errors": [],
      "token_usage_estimate": 8500,
      "timestamp": "2024-06-15T10:30:00"
    }
  ]
}
```

#### GET /api/v1/crew/summary

Get crew summary statistics:

```bash
curl -H "Authorization: Bearer API_KEY" \
  "https://api.workcrew.ai/api/v1/crew/summary?crew=generation"
```

Response:
```json
{
  "ok": true,
  "crew": "generation",
  "summary": {
    "executions": 42,
    "passed": 39,
    "failed": 3,
    "pass_rate": 0.929,
    "avg_duration_ms": 12750.3,
    "min_duration_ms": 8900.2,
    "max_duration_ms": 24150.7
  }
}
```

## Health Monitoring

### Health Status

Components report health status:

```python
from src.observability import HealthChecker

HealthChecker.set_status(
    "llm_service",
    status="healthy",
    message="CrewAI service responding normally",
    checks={
        "model": "llama3.1:8b",
        "latency_ms": 250,
        "error_rate": 0.001
    }
)
```

### Health Endpoints

#### GET /api/v1/system/health

Get overall observability / component health (canonical API liveness is `GET /api/v1/health`):

```bash
curl https://api.workcrew.ai/api/v1/system/health
```

Response:
```json
{
  "overall": "healthy",
  "timestamp": "2024-06-15T10:30:00",
  "components": {
    "database": {
      "name": "database",
      "status": "healthy",
      "message": "PostgreSQL connection pool active",
      "checks": {
        "connections": 10,
        "available": 8
      }
    },
    "llm_service": {
      "name": "llm_service",
      "status": "healthy",
      "message": "CrewAI service responding",
      "checks": {
        "model": "llama3.1:8b",
        "latency_ms": 250
      }
    }
  },
  "healthy": 4,
  "total": 4
}
```

#### GET /api/v1/system/health/{component}

Get specific component health:

```bash
curl -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/system/health/llm_service
```

### Health Status Values

- `healthy` — Component operating normally
- `degraded` — Component partially operational
- `unhealthy` — Component not operational

## Comprehensive Status

### GET /api/v1/status/summary

Get complete system status snapshot:

```bash
curl -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/status/summary
```

Response:
```json
{
  "ok": true,
  "timestamp": "2024-06-15T10:30:00",
  "uptime_seconds": 86400,
  "health": {
    "overall": "healthy",
    "components": {
      "database": {...},
      "llm_service": {...}
    },
    "healthy": 4,
    "total": 4
  },
  "metrics": {
    "counters": {
      "requests_total": 15420,
      "crew_executions_total": 42
    },
    "gauges": {
      "active_connections": 8
    }
  },
  "crew_activity": {
    "executions": 42,
    "passed": 39,
    "failed": 3,
    "pass_rate": 0.929
  }
}
```

## Integration Examples

### Python Client

```python
import requests

API_KEY = "sk_live_abc123"
BASE_URL = "https://api.workcrew.ai"

def get_metrics():
    """Fetch current metrics."""
    r = requests.get(
        f"{BASE_URL}/api/v1/metrics",
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    return r.json()

def get_crew_performance(crew_name):
    """Get crew execution statistics."""
    r = requests.get(
        f"{BASE_URL}/api/v1/crew/summary",
        params={"crew": crew_name},
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    return r.json()["summary"]

# Usage
metrics = get_metrics()
print(f"Average latency: {metrics['histograms']['request_latency_ms']['mean']:.1f}ms")

gen_perf = get_crew_performance("generation")
print(f"Generation pass rate: {gen_perf['pass_rate']:.1%}")
```

### Monitoring Dashboard

Monitor system health in real-time:

```bash
# Watch metrics every 5 seconds
watch -n 5 'curl -s -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/status/summary | jq'
```

### Prometheus Integration

Scrape Prometheus format metrics:

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'workcrew-cms'
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/api/v1/metrics/prometheus'
    bearer_token: 'sk_live_abc123'
```

## Best Practices

### 1. Custom Metrics

Add custom metrics for application logic:

```python
with MetricsCollector.timer("document_generation_ms"):
    generate_document()

MetricsCollector.increment("documents_generated", labels={"type": "blog"})
```

### 2. Request Context

Add context to trace issues:

```python
RequestTracer.set_context("content_id", week_id)
RequestTracer.set_context("operation", "generation")
```

### 3. Crew Instrumentation

Track crew performance:

```python
from src.observability import CrewMetrics, CrewMetricsCollector

metrics = CrewMetrics(
    crew_name=crew.name,
    week_id=week_id,
    task_count=len(tasks),
    agent_count=len(agents),
    total_duration_ms=duration_ms,
    validation_passed=is_valid,
    validation_errors=errors
)
CrewMetricsCollector.record_execution(metrics)
```

### 4. Health Checks

Implement health checks for external services:

```python
def check_llm_health():
    try:
        # Test LLM service
        response = llm.call(test_prompt)
        HealthChecker.set_status("llm_service", "healthy")
    except Exception as e:
        HealthChecker.set_status("llm_service", "unhealthy", str(e))
```

### 5. Alerts

Set up alerts based on metrics:

```python
# Alert if pass rate drops below 90%
crew_summary = CrewMetricsCollector.get_summary("generation")
if crew_summary["pass_rate"] < 0.9:
    send_alert(f"Generation crew pass rate: {crew_summary['pass_rate']:.1%}")
```

## Troubleshooting

### High Latency

Check percentiles:
```bash
curl -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/metrics | jq '.histograms.request_latency_ms'
```

Look for p99 spikes indicating slow requests.

### Crew Failures

Check crew summary:
```bash
curl -H "Authorization: Bearer API_KEY" \
  "https://api.workcrew.ai/api/v1/crew/summary?crew=generation"
```

Review execution history for patterns:
```bash
curl -H "Authorization: Bearer API_KEY" \
  "https://api.workcrew.ai/api/v1/crew/executions?crew=generation&limit=50"
```

### Component Health

Check component status:
```bash
curl -H "Authorization: Bearer API_KEY" \
  https://api.workcrew.ai/api/v1/system/health/llm_service
```

## Performance Tuning

Use metrics to optimize performance:

1. **Identify slow operations** — Review `request_latency_ms` p99
2. **Monitor resource usage** — Track active connections, memory
3. **Optimize crew execution** — Monitor crew duration trends
4. **Validate quality** — Track crew pass rates

## Data Retention

Metrics are kept in-memory. For persistent storage:

```python
from src.observability import MetricsExporter
from pathlib import Path

metrics = MetricsCollector.get_snapshot()
MetricsExporter.save_metrics(metrics, Path("metrics/snapshot.json"))
```

Export periodically to archive metrics for historical analysis.
