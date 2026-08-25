#!/usr/bin/env python
"""Clear demo data while preserving database schema."""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from revenue_os.database import SessionLocal
from revenue_os.analytics.core import AnalyticsEngine


def clear_demo_data():
    """Clear all demo data."""
    db = SessionLocal()

    try:
        # Clear analytics data
        AnalyticsEngine._metrics.clear()
        AnalyticsEngine._dashboards.clear()
        AnalyticsEngine._reports.clear()
        AnalyticsEngine._data_points.clear()

        print("✅ Cleared analytics data")

        # Clear contacts if exists
        try:
            from revenue_os.models.contact import Contact
            db.query(Contact).delete()
            db.commit()
            print("✅ Cleared contacts")
        except:
            pass

        # Clear deals if exists
        try:
            from revenue_os.models.deal import Deal
            db.query(Deal).delete()
            db.commit()
            print("✅ Cleared deals")
        except:
            pass

        print("\n✅ Demo data cleared successfully")
        print("Database schema preserved - ready for new testing")

    except Exception as e:
        print(f"❌ Error clearing demo data: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    print("\n🗑️  Clearing Founder OS Demo Data\n")
    clear_demo_data()
