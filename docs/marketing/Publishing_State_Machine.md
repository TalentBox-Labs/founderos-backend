# Publishing State Machine — Baseline v1.0 (Frozen)

**Status:** FROZEN for the orchestration state names.
**Publication truth (P0):** `published` is remote-write acknowledgement only.
Adapter `ok`, HTTP 200, `rendering_performed=false`, `external_http=false`,
local or static files, and tracker or frontmatter text do not enter `published`.
`verified` stays a later read-back. See
`docs/operations/CONTENT_OPS_P0_LEGACY_PUBLICATION_RECONCILIATION.md`.

**Status:** FROZEN  
**Sprint:** M1.5  
**Date:** 2026-08-09  
**Implementation SoT:** `src/tools/publishing_engine.py`  
**Parent:** [M1_5_PUBLISHING_BASELINE.md](M1_5_PUBLISHING_BASELINE.md)

Governance only. No code changes in M1.5.

---

## Canonical flow (product narrative)

```
Editorial Approved
        ↓
Publish Pending
        ↓
Publishing
        ↓
   ┌────┴────┐
Published   Failed
              ↓
            Retry
```

`cancelled` is an additional terminal state (not in the narrative spine above).

---

## States (frozen string values)

| State | Constant | Role |
|-------|----------|------|
| `editorial_approved` | `STATE_EDITORIAL_APPROVED` | Prerequisite checkpoint (history only; not a durable job.state after create) |
| `publish_pending` | `STATE_PUBLISH_PENDING` | Job created; awaiting manual publish |
| `publishing` | `STATE_PUBLISHING` | Manual publish in progress (adapter invoked) |
| `published` | `STATE_PUBLISHED` | Remote write acknowledgement only |
| `failed` | `STATE_FAILED` | Adapter failed / NOT_IMPLEMENTED |
| `retry` | `STATE_RETRY` | Marked for retry (then immediately re-enters publish) |
| `cancelled` | `STATE_CANCELLED` | Cancelled by human |

---

## Valid transitions

| From | Event | To | Notes |
|------|-------|-----|-------|
| *(none)* | Create job after Editorial approval validated | `publish_pending` | History also records `editorial_approved` checkpoint |
| `publish_pending` | Manual publish | `publishing` → `published` \| `failed` | Atomic command path |
| `failed` | Manual publish | `publishing` → `published` \| `failed` | Allowed without explicit retry |
| `retry` | Manual publish | `publishing` → `published` \| `failed` | |
| `failed` | Retry command | `retry` → (manual publish path) | Increments `retry_count` |
| `retry` | Retry command | `retry` → (manual publish path) | |
| `publish_pending` | Cancel | `cancelled` | |
| `failed` | Cancel | `cancelled` | |
| `retry` | Cancel | `cancelled` | |

---

## Invalid transitions (frozen rejections)

| From | Event | Result |
|------|-------|--------|
| `published` | Manual publish | 409 / LookupError — duplicate |
| `publishing` | Manual publish | 400 — already publishing |
| `cancelled` | Manual publish | 400 |
| `published` | Cancel | 400 |
| `cancelled` | Cancel | 400 |
| `publishing` | Cancel | 400 |
| `publish_pending` | Retry | 400 — retry only from `failed` \| `retry` |
| `published` | Retry | 400 |

---

## Queue membership

Queue (default `GET /jobs`) includes: `publish_pending`, `publishing`, `failed`, `retry`.

Terminal (optional via `include_terminal=true`): also `published`, `cancelled`.

---

## Non-goals (frozen)

- No scheduled transitions
- No Celery / n8n driven transitions
- No AI-initiated transitions
- No automatic publish on Editorial Approval
