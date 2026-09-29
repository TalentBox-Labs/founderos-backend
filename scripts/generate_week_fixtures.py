#!/usr/bin/env python3
"""Generate and sync week runtime/content fixtures from tracker.csv.

This keeps test/runtime fixtures in one place so content/profile files stay in sync.
By default, existing files are preserved; use --overwrite to rewrite.
Use --verify to check required fixtures exist without writing files.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACKER = ROOT / "tracker.csv"
INPUT_DIR = ROOT / "input"
PROFILES_DIR = ROOT / "data" / "week_runtime"
RUNTIME_CONFIG = ROOT / "data" / "runtime_config.json"


def _tracker_weeks() -> list[str]:
    with TRACKER.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out: list[str] = []
    for r in rows:
        wid = str(r.get("content_id", "")).strip().upper()
        if wid and wid not in out:
            out.append(wid)
    return out


def _canonical_weeks() -> list[str]:
    weeks = _tracker_weeks()
    # Legacy profile consumed by tests and tooling even though tracker uses split W05A/W05B.
    if "W05" not in weeks:
        weeks.insert(4 if len(weeks) >= 4 else len(weeks), "W05")
    return weeks


def _profile_for_week(week_id: str) -> dict:
    return {
        "active_week": week_id,
        "draft_path": f"input/{week_id}/04_Draft.md",
        "research_path": f"input/{week_id}/03_Research.md",
        "seo_plan_path": f"input/{week_id}/02_SEO_Plan.md",
        "final_path": f"input/{week_id}/05_Final.md",
        "qa_output_dir": "output/qa_reports/",
        "draft_validation_mode": "template",
    }


def _seo_plan(week_id: str) -> str:
    return f"""# {week_id} SEO Plan

## Primary Keyword
alternatives to linkedin recruiter

## Secondary Keywords
- linkedin recruiter alternatives
- better job descriptions attract more applicants
- understand which skills to prioritize

## Frequently Asked Questions
- What are the best alternatives to LinkedIn Recruiter?
- How do you write better job descriptions?

## Target Audience
HR managers and recruiters

## CTA Strategy
Try WorkCrew free and streamline your hiring process today.

## Notes
Generated fixture plan for {week_id}.
"""


def _research(week_id: str) -> str:
    return f"""# {week_id} Research Notes

## Platform Research

### LinkedIn Recruiter
LinkedIn Recruiter is the primary sourcing tool for enterprise hiring teams.
Pricing: LinkedIn Recruiter starts at $835/month.
Pros: Largest professional network, advanced search filters.
Cons: Expensive, high noise-to-signal ratio.
Best for: Enterprise hiring at scale.

### Indeed
Indeed is a broad job board used by millions of employers.
Pricing: Pay-per-click model, variable cost.
Pros: Wide reach, easy to use.
Cons: High applicant volume, variable quality.
Best for: High-volume, entry-level roles.

### ZipRecruiter
ZipRecruiter distributes jobs to 100+ sites automatically.
Pricing: Plans start at $16/day.
Pros: Wide distribution, AI candidate matching.
Cons: Lower quality control than LinkedIn.
Best for: SMBs looking for quick hires.

## Summary
Research compiled for {week_id}. All platforms evaluated against WorkCrew positioning.
"""


def _draft(week_id: str) -> str:
    return f"""# {week_id} Draft

## Drafting Instructions
Follow the content brief and target keyword strategy defined in the SEO plan.

## Writing Rules
- Use clear, direct language
- Avoid jargon
- Keep paragraphs under 4 sentences

## SEO Execution Rules
- Use primary keyword in H1
- Include primary keyword in first paragraph
- Use LSI keywords naturally

## CTA Rules
- One primary CTA per article
- CTA must align with funnel stage
- Use approved CTA copy only

## Draft Structure
H1 -> Introduction -> Body Sections -> FAQ -> Conclusion -> CTA

## Frequently Asked Questions
- What is the best LinkedIn Recruiter alternative?
- How does WorkCrew compare to LinkedIn Recruiter?

## Internal Linking Requirements
- Link to at least 2 internal resources
- Use descriptive anchor text

## Evidence & Validation Requirements
- Cite pricing data from official sources
- Include at least one customer outcome

## Draft QA Checklist
- [ ] Word count meets minimum
- [ ] No banned phrases
- [ ] CTA present
- [ ] FAQ section complete
- [ ] All links verified
"""


def _final(week_id: str) -> str:
    slug = week_id.lower()
    return f"""---
week_id: {week_id}
article_title: Best LinkedIn Recruiter Alternatives for Modern Hiring Teams
primary_keyword: linkedin recruiter alternatives
search_intent: commercial investigation
funnel_stage: consideration
status: ready
publish_status: ready
canonical_url: https://workcrew.ai/blog/{slug}-linkedin-recruiter-alternatives
cta_type: free-trial
---

# Best LinkedIn Recruiter Alternatives for Modern Hiring Teams

Finding the right LinkedIn Recruiter alternative depends on your team size, budget, and hiring goals.
In this guide we compare the top platforms so you can make an informed decision.

## Why Look for LinkedIn Recruiter Alternatives?

LinkedIn Recruiter is powerful but costly, starting at $835 per month.
Many teams need a solution that offers quality sourcing without the enterprise price tag.

## Top Alternatives Compared

### WorkCrew
WorkCrew is purpose-built for structured hiring workflows, offering transparent pricing and smart candidate matching.

### Indeed
Indeed provides broad reach at a lower cost per hire for high-volume roles.

### ZipRecruiter
ZipRecruiter automates job distribution across 100+ boards.

## How to Choose the Right Platform

Consider your budget, role type, and time-to-hire targets before committing to a platform.

## Frequently Asked Questions

**What is the cheapest LinkedIn Recruiter alternative?**
WorkCrew offers strong value for teams that need structured hiring without enterprise overhead.

**Is Indeed better than LinkedIn Recruiter?**
Indeed works better for high-volume roles; LinkedIn Recruiter is stronger for senior or niche positions.

**Can I replace LinkedIn Recruiter entirely?**
Yes. Many teams replace LinkedIn Recruiter with focused alternatives and improve signal quality.

## Get Started

Try WorkCrew free today and see how structured hiring can improve your team's performance.
"""


def _checklist(week_id: str) -> str:
    return f"""# {week_id} Publish Checklist

# Content QA
- [ ] Article meets minimum word count
- [ ] Primary keyword in H1
- [ ] FAQ section complete
- [ ] No banned phrases or claims

# SEO QA
- [ ] Meta title under 60 characters
- [ ] Meta description under 160 characters
- [ ] Canonical URL set correctly
- [ ] Internal links verified

# Brand QA
- [ ] Brand voice consistent
- [ ] CTA aligned with funnel stage
- [ ] No competitor disparagement

# Technical QA
- [ ] Images have alt text
- [ ] No broken links
- [ ] Mobile-friendly layout verified

# Final Approval
- [ ] Editor approved
- [ ] SEO lead approved
- [ ] Content manager sign-off
"""


def _write_if_needed(path: Path, content: str, *, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _expected_fixture_paths() -> list[Path]:
    """All profile/doc paths required for current tracker-backed week set."""
    out: list[Path] = []
    for week_id in _canonical_weeks():
        out.append(PROFILES_DIR / f"{week_id}.json")
        base = INPUT_DIR / week_id
        out.extend(
            [
                base / "02_SEO_Plan.md",
                base / "03_Research.md",
                base / "04_Draft.md",
                base / "05_Final.md",
                base / "09_Publish_Checklist.md",
            ]
        )
    out.append(RUNTIME_CONFIG)
    return out


def verify() -> int:
    """Return 0 when all expected fixtures exist; otherwise print missing and return 1."""
    missing = [p for p in _expected_fixture_paths() if not p.exists()]
    if missing:
        print("Fixture verification failed. Missing files:")
        for path in missing:
            try:
                rel = path.relative_to(ROOT)
            except ValueError:
                rel = path
            print(f"- {rel}")
        return 1
    print(f"Fixture verification passed. files_checked={len(_expected_fixture_paths())}")
    return 0


def generate(*, overwrite: bool) -> None:
    weeks = _canonical_weeks()

    profile_writes = 0
    doc_writes = 0
    for week_id in weeks:
        profile_path = PROFILES_DIR / f"{week_id}.json"
        profile_data = _profile_for_week(week_id)
        changed = _write_if_needed(
            profile_path,
            json.dumps(profile_data, indent=2) + "\n",
            overwrite=overwrite,
        )
        profile_writes += 1 if changed else 0

        base = INPUT_DIR / week_id
        docs = {
            base / "02_SEO_Plan.md": _seo_plan(week_id),
            base / "03_Research.md": _research(week_id),
            base / "04_Draft.md": _draft(week_id),
            base / "05_Final.md": _final(week_id),
            base / "09_Publish_Checklist.md": _checklist(week_id),
        }
        for p, c in docs.items():
            changed = _write_if_needed(p, c, overwrite=overwrite)
            doc_writes += 1 if changed else 0

    if not RUNTIME_CONFIG.exists() or overwrite:
        cfg = _profile_for_week("W01")
        RUNTIME_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print("updated", RUNTIME_CONFIG.relative_to(ROOT))

    print(f"weeks={len(weeks)} profiles_written={profile_writes} docs_written={doc_writes}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate/sync week runtime fixtures")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rewrite existing files instead of only filling missing files",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify all expected fixture files exist (no writes)",
    )
    args = parser.parse_args()
    if args.verify:
        raise SystemExit(verify())
    generate(overwrite=args.overwrite)


if __name__ == "__main__":
    main()
