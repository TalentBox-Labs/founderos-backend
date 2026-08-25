# Founder OS naming — no new WorkCrew identifiers

## Rule

No new active product code should introduce WorkCrew / WorkCrew CRM /
`WORKCREW_*` / `workcrew_*` identifiers unless explicitly required as a
**backward-compatibility surface**.

## Canonical forms

| Surface | Form |
|---|---|
| Human-facing product | Founder OS |
| Python / internal | `founder_os` |
| Environment variables | `FOUNDER_OS_*` |
| URL / branch / resource (when safely migratable) | `founder-os` |

## Compatibility

Phase 1 preserves legacy `WORKCREW_*` as a deprecated fallback via
`src/tools/env_compat.py` (`getenv_compat`).

Resolution order:

1. `FOUNDER_OS_<SUFFIX>` if present
2. `WORKCREW_<SUFFIX>` if present
3. explicit default

Do not remove `WORKCREW_*` support in Phase 1.
Do not mechanically rewrite historical docs, audits, or frozen contracts.
