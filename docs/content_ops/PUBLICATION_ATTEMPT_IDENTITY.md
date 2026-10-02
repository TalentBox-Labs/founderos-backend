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

Creating a job does not publish. Editorial approval sets `authorizes_publish` false and does not insert an attempt. Adapter success without remote acknowledgement stays unproven. Remote acknowledgement is not verification.

`autonomous_publication_ready()` stays false. Content review on the internal beta surface does not grant autonomous publication.
