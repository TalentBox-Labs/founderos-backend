from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from revenue_os.models.base import Base


class ContactSource(str, enum.Enum):
    MANUAL = "manual"
    IMPORT = "import"
    WEB_FORM = "web_form"
    LINKEDIN = "linkedin"
    REFERRAL = "referral"
    OUTREACH = "outreach"
    API = "api"
    N8N = "n8n"


class ContactStatus(str, enum.Enum):
    LEAD = "lead"
    PROSPECT = "prospect"
    QUALIFIED = "qualified"
    CUSTOMER = "customer"
    CHURNED = "churned"
    PARTNER = "partner"
    CANDIDATE = "candidate"
    VENDOR = "vendor"


class Industry(str, enum.Enum):
    TECHNOLOGY = "technology"
    HEALTHCARE = "healthcare"
    FINANCE = "finance"
    EDUCATION = "education"
    MANUFACTURING = "manufacturing"
    RETAIL = "retail"
    REAL_ESTATE = "real_estate"
    STAFFING = "staffing"
    RECRUITMENT = "recruitment"
    AGENCY = "agency"
    OTHER = "other"


class Company(Base):
    __tablename__ = "companies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    domain = Column(String(255), unique=True, index=True)
    description = Column(Text)
    industry = Column(Enum(Industry), default=Industry.OTHER)
    employee_count = Column(Integer)
    annual_revenue = Column(Float)
    funding_stage = Column(String(100))
    tech_stack = Column(Text)
    hiring_activity = Column(Text)
    linkedin_url = Column(String(500))
    twitter_url = Column(String(500))
    website_url = Column(String(500))
    logo_url = Column(String(500))
    tags = Column(Text)
    notes = Column(Text)
    # NULL stays withheld. A companies list is shown only for this organization.
    organization_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contacts = relationship("Contact", back_populates="company")
    deals = relationship("Deal", back_populates="company")
    projects = relationship("Project", back_populates="company")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True
    )

    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    designation = Column(String(255))
    email = Column(String(255), index=True)
    phone = Column(String(50))
    linkedin_url = Column(String(500))
    twitter_url = Column(String(500))
    source = Column(Enum(ContactSource), default=ContactSource.MANUAL)
    status = Column(Enum(ContactStatus), default=ContactStatus.LEAD)
    lead_score = Column(Integer, default=0)
    tags = Column(Text)
    notes = Column(Text)
    owner_id = Column(UUID(as_uuid=True), nullable=True)
    organization_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_contacted_at = Column(DateTime(timezone=True), nullable=True)

    company = relationship("Company", back_populates="contacts")
    deals = relationship("Deal", back_populates="contact")
    activities = relationship("Activity", back_populates="contact")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
