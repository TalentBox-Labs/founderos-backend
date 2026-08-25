---
tags: [system, ai, reference]
created: 2026-06-11
---

# 🤖 AI Agent Roster

All CrewAI agents in Founder OS.

## Phase 2A — Generation Crew
| Agent | Role | Config File | Output |
|-------|------|-------------|--------|
| Strategist | Content angle, brief | `agents_generation.yaml` | `01_Content_Brief.md` |
| SEO Planner | Keyword strategy | `agents_generation.yaml` | `02_SEO_Plan.md` |
| Researcher | Platform research | `agents_generation.yaml` | `03_Research.md` |
| Writer | Full draft | `agents_generation.yaml` | `04_Draft.md` |

## Phase 2B — Editor Crew
| Agent | Role | Config File | Output |
|-------|------|-------------|--------|
| Editor | Draft → Final polish + YAML FM | `agents_editor.yaml` | `05_Final.md` |

## Phase 3 — QA Crew
| Agent | Role | Config File | Output |
|-------|------|-------------|--------|
| QA Specialist | Audit article for hard observability breaches | `agents.yaml` | QA report |

## Phase 4 — Distribution Crew
| Agent | Role | Config File | Output |
|-------|------|-------------|--------|
| Distribution Agent | Social, email, CMS metadata | `agents_distribution.yaml` | 06–08 files |

## Model Config
- Model: `WORKCREW_CREWAI_MODEL` (default: `ollama/llama3.1:8b`)
- Base URL: `WORKCREW_OLLAMA_BASE_URL` (default: `http://localhost:11434`)
- Temperature: `WORKCREW_CREWAI_TEMPERATURE` (default: `0`)

## Related
- [[Content Pipeline Overview]]
- [[Validator Rules Reference]]
