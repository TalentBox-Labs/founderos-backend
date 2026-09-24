#!/usr/bin/env python3
"""Operator-only HUMAN password recovery (offline; not an HTTP endpoint).

Usage (never paste secrets into shell history as literals)::

  export FOUNDER_OS_RECOVERY_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  # install the same value into staging operator secrets — do not commit it

  python scripts/reset_human_password.py \\
    --email founder-os-staging-operator@example.invalid \\
    --new-password-file /tmp/fos_human_password_recovered.secret \\
    --recovery-secret-file /tmp/fos_recovery_secret.secret \\
    --i-confirm-operator-recovery

Exit codes:
  0 success
  2 authorization / confirmation failure
  3 target / password policy failure
  4 database failure
  5 usage error

Does not print passwords, hashes, or recovery secrets.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Project root on sys.path (matches other scripts/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from revenue_os.database import SessionLocal  # noqa: E402
from revenue_os.services.human_password_recovery import (  # noqa: E402
    RecoveryOutcome,
    recover_human_password,
)


def _read_secret_file(path: Path) -> str:
    raw = path.read_bytes()
    # Single-line credential files; strip one trailing newline only via strip().
    return raw.decode("utf-8").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover password for an existing Founder OS HUMAN account."
    )
    parser.add_argument("--email", required=True, help="Existing HUMAN user email")
    parser.add_argument(
        "--new-password-file",
        required=True,
        type=Path,
        help="0600 file containing the replacement password (not printed)",
    )
    parser.add_argument(
        "--recovery-secret-file",
        required=True,
        type=Path,
        help="File whose contents must match FOUNDER_OS_RECOVERY_SECRET",
    )
    parser.add_argument(
        "--i-confirm-operator-recovery",
        action="store_true",
        help="Required explicit confirmation — refuses without this flag",
    )
    args = parser.parse_args(argv)

    if not args.i_confirm_operator_recovery:
        print("REFUSED outcome=missing_confirmation", file=sys.stderr)
        return 2

    try:
        new_password = _read_secret_file(args.new_password_file)
        recovery_secret = _read_secret_file(args.recovery_secret_file)
    except OSError as exc:
        print(f"REFUSED outcome=usage_error type={type(exc).__name__}", file=sys.stderr)
        return 5

    result = recover_human_password(
        email=args.email,
        new_password=new_password,
        recovery_secret=recovery_secret,
        confirm_operator_recovery=True,
        session_factory=SessionLocal,
    )

    # Never echo email local-part or secrets; domain + outcome only.
    domain = args.email.split("@")[-1] if "@" in args.email else "none"
    if result.ok:
        print(
            f"OK outcome={result.outcome.value} user_id={result.user_id} "
            f"token_version={result.token_version} email_domain={domain}"
        )
        return 0

    print(f"REFUSED outcome={result.outcome.value} email_domain={domain}", file=sys.stderr)
    if result.outcome in {
        RecoveryOutcome.MISSING_RECOVERY_SECRET,
        RecoveryOutcome.WEAK_RECOVERY_SECRET,
        RecoveryOutcome.RECOVERY_SECRET_MISMATCH,
        RecoveryOutcome.MISSING_CONFIRMATION,
    }:
        return 2
    if result.outcome is RecoveryOutcome.DATABASE_FAILURE:
        return 4
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
