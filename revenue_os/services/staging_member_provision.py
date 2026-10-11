"""Offline provisioning of a Founder Organization member.

Does not run on startup. Does not promote users to owner.
Password recovery remains a separate capability and still cannot create users.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session, sessionmaker

from revenue_os.auth import hash_password
from revenue_os.db_url import bootstrap_password_acceptable
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.human_password_recovery import authorize_recovery_secret
from src.tools.editorial_approval import is_human_approver

logger = logging.getLogger(__name__)

MEMBER_ROLE = "member"


class ProvisionOutcome(str, Enum):
    CREATED = "created"
    ALREADY_MEMBER = "already_member"
    MISSING_CONFIRMATION = "missing_confirmation"
    RECOVERY_UNAUTHORIZED = "recovery_unauthorized"
    INVALID_EMAIL = "invalid_email"
    INVALID_NAME = "invalid_name"
    WEAK_PASSWORD = "weak_password"
    ORGANIZATION_NOT_FOUND = "organization_not_found"
    ORGANIZATION_INACTIVE = "organization_inactive"
    PRIVILEGE_ESCALATION_REJECTED = "privilege_escalation_rejected"
    OTHER_TENANT_REJECTED = "other_tenant_rejected"
    DATABASE_FAILURE = "database_failure"


@dataclass(frozen=True)
class ProvisionResult:
    outcome: ProvisionOutcome
    user_id: str | None = None
    email: str | None = None
    organization_slug: str | None = None
    membership_role: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in {ProvisionOutcome.CREATED, ProvisionOutcome.ALREADY_MEMBER}


def _clean_email(email: str) -> str:
    return (email or "").strip().lower()


def _email_ok(email: str) -> bool:
    return bool(email) and "@" in email and " " not in email and len(email) >= 3


def provision_member(
    *,
    email: str,
    full_name: str,
    password: str,
    organization_slug: str,
    recovery_secret: str | None,
    confirm_member_provision: bool,
    session_factory: sessionmaker,
) -> ProvisionResult:
    """Create or reuse one human member of the named organization.

    The membership role is always member. Existing owner or admin memberships
    are left unchanged and the call fails closed.
    """
    if not confirm_member_provision:
        logger.warning("Member provision refused outcome=%s", ProvisionOutcome.MISSING_CONFIRMATION.value)
        return ProvisionResult(outcome=ProvisionOutcome.MISSING_CONFIRMATION)

    auth_fail = authorize_recovery_secret(recovery_secret)
    if auth_fail is not None:
        logger.warning("Member provision refused outcome=%s", ProvisionOutcome.RECOVERY_UNAUTHORIZED.value)
        return ProvisionResult(outcome=ProvisionOutcome.RECOVERY_UNAUTHORIZED)

    target_email = _clean_email(email)
    if not _email_ok(target_email):
        logger.warning("Member provision refused outcome=%s", ProvisionOutcome.INVALID_EMAIL.value)
        return ProvisionResult(outcome=ProvisionOutcome.INVALID_EMAIL)

    name = (full_name or "").strip()
    if not is_human_approver(name):
        logger.warning("Member provision refused outcome=%s", ProvisionOutcome.INVALID_NAME.value)
        return ProvisionResult(outcome=ProvisionOutcome.INVALID_NAME)

    if not bootstrap_password_acceptable(password):
        logger.warning("Member provision refused outcome=%s", ProvisionOutcome.WEAK_PASSWORD.value)
        return ProvisionResult(outcome=ProvisionOutcome.WEAK_PASSWORD)

    slug = (organization_slug or "").strip().lower()
    db: Session = session_factory()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if org is None:
            logger.warning(
                "Member provision refused outcome=%s",
                ProvisionOutcome.ORGANIZATION_NOT_FOUND.value,
            )
            return ProvisionResult(outcome=ProvisionOutcome.ORGANIZATION_NOT_FOUND)
        if org.status != OrganizationStatus.ACTIVE:
            logger.warning(
                "Member provision refused outcome=%s",
                ProvisionOutcome.ORGANIZATION_INACTIVE.value,
            )
            return ProvisionResult(
                outcome=ProvisionOutcome.ORGANIZATION_INACTIVE,
                organization_slug=org.slug,
            )

        user = db.query(User).filter(User.email == target_email).first()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=target_email,
                hashed_password=hash_password(password),
                full_name=name,
                role=MEMBER_ROLE,
                is_active=1,
            )
            db.add(user)
            db.flush()
        elif (user.role or "").strip().lower() in {"owner", "admin"}:
            logger.warning(
                "Member provision refused outcome=%s user_id=%s",
                ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED.value,
                str(user.id),
            )
            db.rollback()
            return ProvisionResult(
                outcome=ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED,
                user_id=str(user.id),
                email=user.email,
                organization_slug=org.slug,
                membership_role=user.role,
            )
        elif not user.is_active or not is_human_approver(user.full_name or ""):
            logger.warning(
                "Member provision refused outcome=%s user_id=%s",
                ProvisionOutcome.INVALID_NAME.value,
                str(user.id),
            )
            return ProvisionResult(
                outcome=ProvisionOutcome.INVALID_NAME,
                user_id=str(user.id),
                email=user.email,
            )

        memberships = (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.user_id == user.id)
            .all()
        )
        intended = [row for row in memberships if row.organization_id == org.id]
        others = [row for row in memberships if row.organization_id != org.id]
        if others:
            logger.warning(
                "Member provision refused outcome=%s user_id=%s",
                ProvisionOutcome.OTHER_TENANT_REJECTED.value,
                str(user.id),
            )
            db.rollback()
            return ProvisionResult(
                outcome=ProvisionOutcome.OTHER_TENANT_REJECTED,
                user_id=str(user.id),
                email=user.email,
                organization_slug=org.slug,
            )
        if intended:
            current = (intended[0].role or "").strip().lower()
            if current != MEMBER_ROLE:
                logger.warning(
                    "Member provision refused outcome=%s user_id=%s",
                    ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED.value,
                    str(user.id),
                )
                db.rollback()
                return ProvisionResult(
                    outcome=ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED,
                    user_id=str(user.id),
                    email=user.email,
                    organization_slug=org.slug,
                    membership_role=current,
                )
            if user.role != MEMBER_ROLE:
                logger.warning(
                    "Member provision refused outcome=%s user_id=%s",
                    ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED.value,
                    str(user.id),
                )
                db.rollback()
                return ProvisionResult(
                    outcome=ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED,
                    user_id=str(user.id),
                    email=user.email,
                    organization_slug=org.slug,
                    membership_role=user.role,
                )
            db.commit()
            logger.info(
                "Member provision already present user_id=%s outcome=%s",
                str(user.id),
                ProvisionOutcome.ALREADY_MEMBER.value,
            )
            return ProvisionResult(
                outcome=ProvisionOutcome.ALREADY_MEMBER,
                user_id=str(user.id),
                email=user.email,
                organization_slug=org.slug,
                membership_role=MEMBER_ROLE,
            )

        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=org.id,
                role=MEMBER_ROLE,
                status=MembershipStatus.ACTIVE,
            )
        )
        if (user.role or "").strip().lower() != MEMBER_ROLE:
            logger.warning(
                "Member provision refused outcome=%s user_id=%s",
                ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED.value,
                str(user.id),
            )
            db.rollback()
            return ProvisionResult(
                outcome=ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED,
                user_id=str(user.id),
                email=user.email,
                organization_slug=org.slug,
                membership_role=user.role,
            )
        db.commit()
        logger.info(
            "Member provision created user_id=%s outcome=%s",
            str(user.id),
            ProvisionOutcome.CREATED.value,
        )
        return ProvisionResult(
            outcome=ProvisionOutcome.CREATED,
            user_id=str(user.id),
            email=user.email,
            organization_slug=org.slug,
            membership_role=MEMBER_ROLE,
        )
    except Exception:
        db.rollback()
        logger.exception("Member provision failed outcome=%s", ProvisionOutcome.DATABASE_FAILURE.value)
        return ProvisionResult(outcome=ProvisionOutcome.DATABASE_FAILURE)
    finally:
        db.close()
