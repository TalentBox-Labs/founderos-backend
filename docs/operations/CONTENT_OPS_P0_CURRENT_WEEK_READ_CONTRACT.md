# Content Ops P0 current-week read

`GET /api/v1/content-ops/weeks/current`

Tenant model: `EXPLICIT_SINGLE_ORG_BETA_LOCK`. The caller must be an authenticated human member of the server beta organization. Service credentials and client organization fields are denied. This route does not publish, approve, generate, or schedule.

## Week selection

Source: `data/runtime_config.json` field `active_week`.

`WORKCREW_RUNTIME_CONFIG`, query, header, cookie, and body are ignored. A week id is accepted only when it matches `W` plus two digits and an optional letter, and exactly one `tracker.csv` row has that `content_id`.

| Condition | HTTP | Body |
|---|---|---|
| No `active_week` | 200 | `state=empty`, `reason=no_current_week`, `week_id=null` |
| Malformed `active_week` | 200 | `state=empty`, `reason=malformed_current_week` |
| Id not in the tracker | 200 | `state=empty`, `reason=current_week_not_in_tracker` |
| Duplicate tracker id | 409 | `error.code=ACTIVE_INTENT` |
| Any job `state=publishing` | 409 | `error.code=ACTIVE_INTENT` |
| Unauthenticated | 401 | existing human-session denial |
| Service, non-member, zero membership, multi-org without a pin, other organization | 403 | existing beta-organization denial |

There is no client payload, so this read does not return 422. Unexpected failures stay 5xx. Denial is not an empty 200.

## Publication mapping

`publication_truth` and `verification_status` use the publication-truth vocabulary: `unproven`, `remote_write_confirmed`, `verification_pending`, `verified`, `failed`, `unknown_remote`.

A job state of `published`, a tracker status, frontmatter, an adapter `ok`, an HTTP status, a local file, or a URL does not select `verified`. `verified` requires stored read-back evidence (`readback_performed`, remote object id, content hash, `readback_matches`). A confirmed remote write stays `verification_pending`. `published_url` is returned only for `remote_write_confirmed` or `verified`, and only from the job's `published_url` or `remote_url`. `open_published_url` is listed only when the read-back is `verified`.

`next_schedule_at` is always null. No scheduler is advertised.

`risk_class` repeats the publication-truth risk (`unproven`, `verification_pending`, `failed`, `unknown_remote`, `none`). It is not a separate risk engine.

## Allowed actions

Server-derived, in this order when the underlying route exists for the current state: `run_now`, `approve`, `reject`, `request_changes`, `retry`, `cancel`, `open_artifact`, `open_published_url`, `view_audit`.

`run_now` means the existing human `POST /generate` while the week is still a draft. `approve` is included only when a staging bundle for a promotion phase is present. `retry` and `cancel` are included only for one eligible publish job, and are omitted when the remote result is unknown. `pause_week` and `resume` are omitted.

`publication_job_id` is an extra field naming the job the publication fields were taken from. It is not client input.

## Agent C adaptation

The Beta UI mock can keep the same projection field names. It cannot keep its mock enums. Replace `PUBLISHED`, `NOT_PUBLISHED`, `UNVERIFIED`, `PENDING`, and `UNKNOWN` with the publication-truth tokens above. Drop `pause_week`, `resume`, `cancel_run`, and `retry_failed_stage` unless a later gate implements those routes. Map live HTTP 401 and 403 onto the page error state; this route does not wrap those denials in a 200 envelope.
