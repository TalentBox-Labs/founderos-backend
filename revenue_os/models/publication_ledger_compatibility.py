"""Durable publication-ledger compatibility marker.

One row records that publication authority is the application PostgreSQL
ledger at a specific generation. The marker lives in the database, so it
survives container replacement. Publication code must refuse to run when
this row is missing or does not match the binary.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String

from revenue_os.models.base import Base


class PublicationLedgerCompatibility(Base):
    """Singleton compatibility row. ``singleton_id`` is always 1."""

    __tablename__ = "publication_ledger_compatibility"

    singleton_id = Column(Integer, primary_key=True, autoincrement=False)
    ledger_generation = Column(Integer, nullable=False)
    authority = Column(String(32), nullable=False)
