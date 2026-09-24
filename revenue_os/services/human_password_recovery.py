"""Operator-controlled HUMAN password recovery (offline capability).

Authority model
---------------
Recovery authority is ``FOUNDER_OS_RECOVERY_SECRET`` only — compared via
constant-time equality against an operator-supplied secret.

This capability is intentionally **not** granted by:

- ordinary HUMAN session authentication
- ordinary SERVICE / ``RUNNER_API_KEY`` authentication
- tenant / organization membership
- client body/query/header org or tenant assertions
- OAuth state
- Gmail credentials
- ``SECRET_KEY``, ``DATABASE_URL``, or bootstrap password values

Network exposure
----------------
There is no HTTP recovery route. Operators invoke
``scripts/reset_human_password.py`` (or call ``recover_human_password`` from
an equivalent offline harness). Remote rate limiting is not applicable.

Session revocation semantics
----------------------------
On success, ``User.token_version`` is incremented in the same DB transaction
as the password hash update. HUMAN identity cookies carry JWT claim ``tv``;
``identity_from_request`` rejects sessions whose ``tv`` does not match the
current ``token_version``. Pre-recovery sessions (including other devices)
therefore lose HUMAN authority without needing known JTIs.

Audit
-----
Success/failure logs include ``user_id`` and a safe outcome code only.
Never log passwords, hashes, or the recovery secret.
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session, sessionmaker

from revenue_os.auth import hash_password
from revenue_os.db_url import (
    bootstrap_password_acceptable,
    sanitize_exception_for_log,
    validate_recovery_secret,
)
from revenue_os.models.user import User
from src.tools.editorial_approval import is_human_approver

logger = logging.getLogger(__name__)

ENV_RECOVERY_SECRET = "FOUNDER_OS_RECOVERY_SECRET"


class RecoveryOutcome(str, Enum):
    SUCCESS = "success"
    MISSING_RECOVERY_SECRET = "missing_recovery_secret"
    WEAK_RECOVERY_SECRET = "weak_recovery_secret"
    RECOVERY_SECRET_MISMATCH = "recovery_secret_mismatch"
    MISSING_CONFIRMATION = "missing_confirmation"
    MISSING_TARGET = "missing_target"
    AMBIGUOUS_TARGET = "ambiguous_target"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_INACTIVE = "target_inactive"
    TARGET_NOT_HUMAN = "target_not_human"
    WEAK_NEW_PASSWORD = "weak_new_password"
    DATABASE_FAILURE = "database_failure"


@dataclass(frozen=True)
class RecoveryResult:
    outcome: RecoveryOutcome
    user_id: str | None = None
    email: str | None = None
    token_version: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is RecoveryOutcome.SUCCESS


def _configured_recovery_secret() -> str | None:
    raw = os.environ.get(ENV_RECOVERY_SECRET)
    if raw is None:
        return None
    return raw


def authorize_recovery_secret(provided: str | None) -> RecoveryOutcome | None:
    """Return a failure outcome, or None when authorization succeeds."""
    configured = _configured_recovery_secret()
    if configured is None or not str(configured).strip():
        return RecoveryOutcome.MISSING_RECOVERY_SECRET
    try:
        expected = validate_recovery_secret(configured)
    except ValueError:
        return RecoveryOutcome.WEAK_RECOVERY_SECRET
    if provided is None:
        return RecoveryOutcome.RECOVERY_SECRET_MISMATCH
    provided_clean = provided.strip()
    if not provided_clean:
        return RecoveryOutcome.RECOVERY_SECRET_MISMATCH
    if not hmac.compare_digest(provided_clean, expected):
        return RecoveryOutcome.RECOVERY_SECRET_MISMATCH
    return None


def recover_human_password(
    *,
    email: str,
    new_password: str,
    recovery_secret: str | None,
    confirm_operator_recovery: bool,
    session_factory: sessionmaker,
) -> RecoveryResult:
    """Recover password for an existing HUMAN user.

    Does not create users, change email/role/membership, or accept SERVICE/
    HUMAN-session authority. Requires explicit operator confirmation flag.
    """
    if not confirm_operator_recovery:
        logger.warning("HUMAN password recovery refused outcome=%s", RecoveryOutcome.MISSING_CONFIRMATION.value)
        return RecoveryResult(outcome=RecoveryOutcome.MISSING_CONFIRMATION)

    auth_fail = authorize_recovery_secret(recovery_secret)
    if auth_fail is not None:
        logger.warning("HUMAN password recovery refused outcome=%s", auth_fail.value)
        return RecoveryResult(outcome=auth_fail)

    target_email = (email or "").strip().lower()
    if not target_email or "@" not in target_email or " " in target_email:
        logger.warning("HUMAN password recovery refused outcome=%s", RecoveryOutcome.MISSING_TARGET.value)
        return RecoveryResult(outcome=RecoveryOutcome.MISSING_TARGET)

    if not bootstrap_password_acceptable(new_password):
        logger.warning("HUMAN password recovery refused outcome=%s", RecoveryOutcome.WEAK_NEW_PASSWORD.value)
        return RecoveryResult(outcome=RecoveryOutcome.WEAK_NEW_PASSWORD)

    db: Session = session_factory()
    try:
        matches = (
            db.query(User)
            .filter(User.email == target_email)
            .order_by(User.created_at.asc())
            .all()
        )
        if not matches:
            logger.warning(
                "HUMAN password recovery refused outcome=%s email_domain=%s",
                RecoveryOutcome.TARGET_NOT_FOUND.value,
                target_email.split("@")[-1],
            )
            return RecoveryResult(outcome=RecoveryOutcome.TARGET_NOT_FOUND)
        if len(matches) > 1:
            # Unique constraint should prevent this; fail closed if violated.
            logger.error(
                "HUMAN password recovery refused outcome=%s match_count=%s",
                RecoveryOutcome.AMBIGUOUS_TARGET.value,
                len(matches),
            )
            return RecoveryResult(outcome=RecoveryOutcome.AMBIGUOUS_TARGET)

        user = matches[0]
        if not user.is_active:
            logger.warning(
                "HUMAN password recovery refused outcome=%s user_id=%s",
                RecoveryOutcome.TARGET_INACTIVE.value,
                str(user.id),
            )
            return RecoveryResult(outcome=RecoveryOutcome.TARGET_INACTIVE, user_id=str(user.id))

        if not is_human_approver(user.full_name or ""):
            logger.warning(
                "HUMAN password recovery refused outcome=%s user_id=%s",
                RecoveryOutcome.TARGET_NOT_HUMAN.value,
                str(user.id),
            )
            return RecoveryResult(outcome=RecoveryOutcome.TARGET_NOT_HUMAN, user_id=str(user.id))

        prior_version = int(user.token_version or 0)
        user.hashed_password = hash_password(new_password)
        user.token_version = prior_version + 1
        db.commit()
        logger.info(
            "HUMAN password recovered user_id=%s token_version=%s outcome=%s",
            str(user.id),
            user.token_version,
            RecoveryOutcome.SUCCESS.value,
        )
        return RecoveryResult(
            outcome=RecoveryOutcome.SUCCESS,
            user_id=str(user.id),
            email=user.email,
            token_version=int(user.token_version),
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            "HUMAN password recovery failed outcome=%s type=%s",
            RecoveryOutcome.DATABASE_FAILURE.value,
            sanitize_exception_for_log(exc),
        )
        return RecoveryResult(outcome=RecoveryOutcome.DATABASE_FAILURE)
    finally:
        db.close()


def parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
