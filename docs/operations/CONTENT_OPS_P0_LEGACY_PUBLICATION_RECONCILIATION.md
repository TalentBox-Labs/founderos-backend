# Content Ops P0 legacy publication reconciliation

Date: 2026-09-29
Base: Main `b1b71700f94978d63c4f93c1e94afbfa371cf3e3`
Scope: inventory only. No remote CMS read, write, unpublish, or delete.

Publication jobs under `output/publishing/` are not in git.
No stored remote object id or provider acknowledgement was found.
Frontmatter `status: published` and `publish_status: published` were left unchanged.
Tracker rows were left unchanged. Tracker `status` is `QA Passed` on every row, which is not remote proof.

Classification key:

- `REMOTE_EXISTENCE_PROVEN` — a stored provider object id or acknowledgement exists in the repo
- `REMOTE_EXISTENCE_UNPROVEN` — a URL or published token exists, and no receipt exists
- `LOCAL_ONLY` — local files only, with no published claim
- `RECONCILIATION_REQUIRED` — a published claim disagrees with tracker state or lacks a receipt

Every inventoried week is `RECONCILIATION_REQUIRED`. Remote existence is unproven. Do not unpublish them in a later gate until a read-back receipt is recorded.

| content_id | tracker current_step | frontmatter | final file | class | remote existence |
|---|---|---|---|---|---|
| W01 | Completed | published | input/W01/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W02 | Publish Review | published | input/W02/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W03 | Completed | published | input/W03/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W04 | Publish Review | published | input/W04/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W05 | not a tracker row | published | input/W05/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W05A | Publish Review | same file as W05 | input/W05/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W05B | Publish Review | same file as W05 | input/W05/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W06A | Publish Review | published | input/W06A/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W06B | Publish Review | published | input/W06B/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W07A | Publish Review | published | input/W07A/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W07B | Publish Review | published | input/W07B/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W09A | Completed | published | input/W09A/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W09B | Completed | published | input/W09B/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |
| W10 | Publish Review | published | input/W10/05_Final.md | RECONCILIATION_REQUIRED | REMOTE_EXISTENCE_UNPROVEN |

`LOCAL_ONLY` count in this inventory: 0. The static website provider can write local files, and those writes are not publication.

`REMOTE_EXISTENCE_PROVEN` count: 0.

Later cleanup may attach a real read-back receipt or correct the local claim. It must not delete or unpublish a remote object from this manifest alone.
