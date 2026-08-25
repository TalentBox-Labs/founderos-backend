---
tags: [system, pipeline, reference]
created: 2026-06-11
---

# ⚡ Content Pipeline Overview

This note maps every phase of the Founder OS content lifecycle.

## Phases

```
Brief → SEO Plan → Research → Draft → Editor → QA → Checklist → Go-Live → Sheet Sync
  01       02         03        04      05      🤖      09         🚀         📊
```

### Phase 1 — Strategy & Brief
- [[01_Content_Brief]] — Topic, audience, funnel stage, CTA
- Owner: Strategist
- Outputs: `01_Content_Brief.md`

### Phase 2A — Generation (CrewAI)
- [[SEO Framework]] — Keyword, intent, canonical URL
- [[Research Gate Rules]] — Required terms per validator
- Agents: Strategist → SEO Planner → Researcher → Writer
- Outputs: `02_SEO_Plan.md`, `03_Research.md`, `04_Draft.md`

### Phase 2B — Editor (CrewAI)
- Editor agent polishes draft into publish-ready `05_Final.md`
- Writes YAML front matter: `week_id`, `canonical_url`, `cta_type`, `publish_status`
- See: [[Final Front Matter Schema]]

### Phase 3 — Validation Gates
- [[QA Gate: research_mapper]] — Research & SEO term coverage
- [[QA Gate: draft_validator]] — Structure, word count, banned phrases
- [[QA Gate: structure_checker]] — H1, H2, FAQ presence
- [[QA Gate: metadata_checker]] — YAML front matter completeness
- [[QA Gate: publish_checklist_checker]] — 09_Publish_Checklist.md sections

### Phase 4A — Go-Live
- Human review of checklist
- `go_live_helpers.record-live` records confirmed URL in repo

### Phase 4B — Sheet Sync
- `sheet_sync --write` mirrors tracker to Google Sheets 22-column board
- See: [[Google Sheets Integration]]

## Related
- [[AI Agent Roster]]
- [[Validator Rules Reference]]
- [[Week Template]]
