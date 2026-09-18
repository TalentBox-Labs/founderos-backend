from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from revenue_os.config import settings
from revenue_os.db_url import assert_pool_within_limit

# Hard ceiling for a single Render Free instance against Neon Free.
POOL_SIZE = 10
MAX_OVERFLOW = 20
assert_pool_within_limit(POOL_SIZE, MAX_OVERFLOW, limit=30)

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DATABASE_ECHO,
    pool_pre_ping=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create missing tables from canonical SQLAlchemy metadata.

    Idempotent: existing tables are left in place. Does not drop, seed,
    migrate, or mutate environment.
    """
    import revenue_os.models  # noqa: F401 — register models on Base.metadata
    from revenue_os.models.base import Base

    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
