# Publication attempt identity

A publication attempt is one logical write opportunity. It is not a random job id.

The identity is:

```text
tenant_id
+ content_id
+ content_version
+ channel
+ destination
```

`tenant_id` is the organization returned by the Content Ops human gate. Request body, query, and header organization fields are not part of the identity.

`content_version` is the SHA-256 of the approved `05_Final.md` when that file exists. Otherwise it is the editorial decision id that passed the readiness check. A different artifact hash is a different attempt.

`destination` is `{channel}:default`. The publishing engine has one server destination per registered channel. Callers cannot supply a destination string.

The attempt is stored in the application database table `publication_attempts`. The primary key is the identity tuple. `create_publish_job` inserts that row in one database transaction. A conflicting insert returns the existing job and does not call a channel adapter. `output/publishing/publication_attempts.sqlite` is not an authority. Job JSON under `output/publishing/` is a cache; durable state on the row wins after a restart.

A singleton row in `publication_ledger_compatibility` records ledger generation `2` and authority `postgresql`. This binary seals that row when it is absent. A missing, unreadable, or different generation fails closed and does not open the retired SQLite file. Images that do not contain `src.tools.publication_ledger_guard` fail the Render pre-deploy command and do not become the live process.

An inert sentinel is a `publication_attempts` row with `attempt_class=inert_sentinel`. The database check `ck_publication_attempts_inert_remote` rejects a remote-write flag or any publication truth other than `inert_non_publishable` on that class. The server identity is fixed (`INERT-SENTINEL`, `inert-sentinel:v1`, channel `inert`, destination `inert:non-publishable`). Callers cannot supply it, and `manual_publish` rejects the class before an adapter is selected. The sentinel is not editorial content and is not written into the job cache.

Creating a job does not publish. Editorial approval sets `authorizes_publish` false and does not insert an attempt. Adapter success without remote acknowledgement stays unproven. Remote acknowledgement is not verification.

`autonomous_publication_ready()` stays false. Content review on the internal beta surface does not grant autonomous publication.
