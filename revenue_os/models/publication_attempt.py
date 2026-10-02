"""Durable publication-attempt authority.

One row is one logical publication intent. The identity columns are the
uniqueness authority. Job JSON under output/publishing is a cache of the
same attempt, not a second ledger.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, String, UniqueConstraint

from revenue_os.models.base import Base


class PublicationAttempt(Base):
    """Server-derived publication identity stored in the application database."""

    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_publication_attempts_job_id"),
        CheckConstraint(
            "(attempt_class <> 'inert_sentinel') OR "
            "((NOT remote_write_outstanding) AND publication_truth = 'inert_non_publishable')",
            name="ck_publication_attempts_inert_remote",
        ),
    )

    tenant_id = Column(String(80), primary_key=True, nullable=False)
    content_id = Column(String(80), primary_key=True, nullable=False)
    content_version = Column(String(200), primary_key=True, nullable=False)
    channel = Column(String(40), primary_key=True, nullable=False)
    destination = Column(String(80), primary_key=True, nullable=False)
    job_id = Column(String(80), nullable=False)
    created_at = Column(String(40), nullable=False)
    requested_by = Column(String(200), nullable=False)
    state = Column(String(40), nullable=False)
    publication_truth = Column(String(40), nullable=False)
    verification_status = Column(String(40), nullable=False)
    remote_write_outstanding = Column(Boolean, nullable=False, default=False)
    attempt_class = Column(
        String(40),
        nullable=False,
        default="publication",
        server_default="publication",
    )
