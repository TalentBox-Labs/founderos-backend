#!/usr/bin/env python3
"""Provision one staging human as a member of an existing organization.

Does not print the password. Does not create owner or admin memberships.

Usage::

  python scripts/provision_staging_member.py \\
    --email t01.internal-beta@fixture.invalid \\
    --full-name "Internal Beta Tester One" \\
    --organization-slug founder-organization \\
    --password-file /tmp/t01.secret \\
    --recovery-secret-file /tmp/fos_recovery_secret.secret \\
    --i-confirm-member-provision
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from revenue_os.database import SessionLocal  # noqa: E402
from revenue_os.services.staging_member_provision import (  # noqa: E402
    provision_member,
)


def _read_secret_file(path: Path) -> str:
    return path.read_bytes().decode("utf-8").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision a staging member. Never prints passwords.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--organization-slug", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--recovery-secret-file", required=True, type=Path)
    parser.add_argument("--i-confirm-member-provision", action="store_true")
    args = parser.parse_args(argv)

    if not args.i_confirm_member_provision:
        print("REFUSED outcome=missing_confirmation", file=sys.stderr)
        return 2
    try:
        password = _read_secret_file(args.password_file)
        recovery_secret = _read_secret_file(args.recovery_secret_file)
    except OSError as exc:
        print(f"REFUSED outcome=usage_error type={type(exc).__name__}", file=sys.stderr)
        return 5

    result = provision_member(
        email=args.email,
        full_name=args.full_name,
        password=password,
        organization_slug=args.organization_slug,
        recovery_secret=recovery_secret,
        confirm_member_provision=True,
        session_factory=SessionLocal,
    )
    print(
        "outcome={outcome} user_id={user_id} role={role} tenant_slug={slug}".format(
            outcome=result.outcome.value,
            user_id=result.user_id or "",
            role=result.membership_role or "",
            slug=result.organization_slug or "",
        )
    )
    if result.ok:
        return 0
    if result.outcome.value in {"missing_confirmation", "recovery_unauthorized"}:
        return 2
    if result.outcome.value == "database_failure":
        return 4
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
