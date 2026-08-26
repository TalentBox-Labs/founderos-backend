"""Marketing campaign persistence — canonical campaign instance SoT.

MarketingCampaign.id is the stable identifier referenced by channel
ApprovalRequests (email / WhatsApp / LinkedIn / social). Pre-approval
drafts use status=\"planning\".
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from revenue_os.models.base import Base


class MarketingCampaign(Base):
    """Cross-channel campaign coordination instance.

    Channels/KPIs are plain text (comma-separated) to avoid a JSON column
    dependency the rest of this codebase does not use.
    """

    __tablename__ = "marketing_campaigns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    goal = Column(Text, default="")
    channels = Column(Text, default="")  # comma-separated
    status = Column(String(20), nullable=False, default="planning")  # planning|active|completed
    kpis = Column(Text, default="")
    owner_agents = Column(Text, default="")
    start_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
