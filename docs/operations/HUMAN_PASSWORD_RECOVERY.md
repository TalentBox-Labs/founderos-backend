# HUMAN password recovery (operator-controlled)

## Problem

Exact Main exposes `POST /api/v1/identity/password` as **authenticated HUMAN
self-service password change** requiring the current password. That endpoint
must not be repurposed as recovery when the current password is unavailable.

## Mechanism

Offline operator CLI:

```bash
python scripts/reset_human_password.py \
  --email <existing-human-email> \
  --new-password-file /path/to/0600-new-password \
  --recovery-secret-file /path/to/0600-recovery-secret \
  --i-confirm-operator-recovery
```

Core logic: `revenue_os.services.human_password_recovery.recover_human_password`.

## Authority model

Recovery authority is **only** `FOUNDER_OS_RECOVERY_SECRET` (validated with
`validate_recovery_secret`, compared via `hmac.compare_digest`).

Not recovery authority:

- ordinary HUMAN session
- `RUNNER_API_KEY` / SERVICE
- tenant membership
- body/query/header org or tenant assertions
- OAuth state
- Gmail credentials
- `SECRET_KEY`, `DATABASE_URL`, bootstrap password

There is **no** HTTP recovery route.

## Target semantics

- Existing user only (email unique lookup)
- Fail closed: missing, inactive, non-HUMAN display name, ambiguous matches
- Does not create/delete users
- Does not change email, role, or organization membership

## Password policy / hashing

Reuses `bootstrap_password_acceptable` and `hash_password` / `verify_password`.

## Session revocation semantics

`User.token_version` is incremented in the same DB transaction as the hash
update. HUMAN JWTs carry claim `tv`. `identity_from_request` rejects sessions
whose `tv` does not match the current `token_version`.

Self-service password change also bumps `token_version` (and still stages the
current cookie JTI).

**RECOVERY_SESSION_REVOCATION_SEMANTICS:** all pre-recovery HUMAN identity
sessions for that account lose authority via `token_version` mismatch,
without needing known JTIs.

## Audit

Logs outcome codes and `user_id` / `token_version` only. Never logs passwords,
hashes, or the recovery secret.

## Staging deployment notes (separate gate)

1. Generate a staging-only recovery secret (≥32 chars, not a placeholder).
2. Install as Render env `FOUNDER_OS_RECOVERY_SECRET` (do not commit).
3. Deploy this SHA.
4. Run the CLI against staging with 0600 password/secret files.
5. Prove login → identity/me → tenant/me → logout → fresh login.

This document does not authorize merge, deploy, or live recovery.
