#!/usr/bin/env python3
"""Seed a bounded commercial fixture into one existing organization.

Reads local HubSpot exports. Does not write back to HubSpot or Clay.
Does not print source personal data.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from revenue_os.database import SessionLocal  # noqa: E402
from revenue_os.models.organization import Organization  # noqa: E402
from revenue_os.services.internal_beta_commercial_fixture import (  # noqa: E402
    FIXTURE_VERSION,
    seed_commercial_fixture,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the internal-beta commercial fixture.")
    parser.add_argument("--organization-slug", required=True)
    parser.add_argument("--hubspot-companies-csv", type=Path)
    parser.add_argument("--hubspot-contacts-csv", type=Path)
    parser.add_argument("--clay-json", type=Path)
    parser.add_argument(
        "--fixture-stdin",
        action="store_true",
        help="Read a minimized JSON object from stdin. Does not print the payload.",
    )
    parser.add_argument("--i-confirm-staging-fixture", action="store_true")
    args = parser.parse_args(argv)
    if not args.i_confirm_staging_fixture:
        print("REFUSED outcome=missing_confirmation", file=sys.stderr)
        return 2
    try:
        if args.fixture_stdin:
            payload = json.load(sys.stdin)
            hubspot_companies = list(payload.get("hubspot_companies") or [])
            hubspot_contacts = list(payload.get("hubspot_contacts") or [])
            clay_prospects = list(payload.get("clay_prospects") or [])
        else:
            if not args.hubspot_companies_csv or not args.hubspot_contacts_csv or not args.clay_json:
                print("REFUSED outcome=usage_error", file=sys.stderr)
                return 5
            hubspot_companies = _read_csv(args.hubspot_companies_csv)
            hubspot_contacts = _read_csv(args.hubspot_contacts_csv)
            clay_prospects = list(json.loads(args.clay_json.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        print(f"REFUSED outcome=usage_error type={type(exc).__name__}", file=sys.stderr)
        return 5

    db = SessionLocal()
    try:
        org = (
            db.query(Organization)
            .filter(Organization.slug == args.organization_slug.strip().lower())
            .first()
        )
        if org is None:
            print("REFUSED outcome=organization_not_found", file=sys.stderr)
            return 3
        counts = seed_commercial_fixture(
            db,
            organization_id=org.id,
            hubspot_companies=hubspot_companies,
            hubspot_contacts=hubspot_contacts,
            clay_prospects=clay_prospects,
            confirm_staging_fixture=True,
        )
    except Exception as exc:
        print(f"REFUSED outcome=database_failure type={type(exc).__name__}", file=sys.stderr)
        return 4
    finally:
        db.close()
    print(
        "fixture_version={version} companies={companies} contacts={contacts} "
        "prospects={prospects} leads={leads} qualified={qualified} "
        "opportunities={opportunities} customers={customers} activities={activities} "
        "unmapped_lifecycle={unmapped}".format(
            version=FIXTURE_VERSION,
            companies=counts.companies,
            contacts=counts.contacts,
            prospects=counts.prospects,
            leads=counts.leads,
            qualified=counts.qualified,
            opportunities=counts.opportunities,
            customers=counts.customers,
            activities=counts.activities,
            unmapped=counts.unmapped_lifecycle,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
