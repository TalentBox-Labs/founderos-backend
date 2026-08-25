#!/usr/bin/env python
"""Initialize demo database with sample data for testing."""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Never
import uuid

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from revenue_os.database import SessionLocal, init_db
from revenue_os.analytics.core import (
    AnalyticsEngine,
    AnalyticsMetric,
    MetricType,
    DashboardType,
)
from revenue_os.analytics.dashboards import DashboardBuilder


def _assert_never(value: Never) -> Never:
    raise TypeError(f"Unhandled demo metric type: {value}")


def demo_metric_base_target(metric: AnalyticsMetric) -> float:
    """Deterministic demo baseline. Preserves target_value when present."""
    if metric.target_value is not None:
        return float(metric.target_value)
    metric_type = metric.metric_type
    if metric_type is MetricType.REVENUE:
        return 1_000_000.0
    if metric_type is MetricType.PERCENTAGE:
        return 50.0
    if metric_type is MetricType.TIME:
        return 45.0
    if metric_type is MetricType.COUNT:
        return 100.0
    if metric_type is MetricType.RATIO:
        return 75.0
    if metric_type is MetricType.CUSTOM:
        return 50.0
    _assert_never(metric_type)


def demo_metric_sample_value(metric: AnalyticsMetric, day_offset: int) -> float:
    """Type-aware demo sample value. Safe when target_value is None."""
    jitter_key = hash(str(day_offset))
    if metric.metric_type is MetricType.REVENUE:
        value = 750000 + (day_offset * 5000) + (jitter_key % 50000)
    elif metric.metric_type is MetricType.PERCENTAGE:
        value = demo_metric_base_target(metric) + ((jitter_key % 20) - 10)
    elif metric.metric_type is MetricType.TIME:
        value = demo_metric_base_target(metric) + ((jitter_key % 10) - 5)
    elif metric.metric_type is MetricType.COUNT:
        value = demo_metric_base_target(metric) + ((jitter_key % 20) - 10)
    elif metric.metric_type is MetricType.RATIO:
        value = demo_metric_base_target(metric) + ((jitter_key % 40) - 20)
    elif metric.metric_type is MetricType.CUSTOM:
        value = demo_metric_base_target(metric) + (jitter_key % 40)
    else:
        _assert_never(metric.metric_type)
    return max(0.0, float(value))


def init_database():
    """Initialize database schema."""
    try:
        init_db()
        print("✅ Database schema initialized")
    except Exception as e:
        print(f"⚠️  Database already initialized or error: {e}")


def create_demo_metrics():
    """Create demo metrics."""
    db = SessionLocal()

    metrics_data = [
        {
            "name": "Total ARR",
            "metric_type": MetricType.REVENUE,
            "calculation": "SUM(deals.arr_value WHERE status='closed')",
            "unit": "$",
            "description": "Annual Recurring Revenue from closed deals",
            "target_value": 1000000.0,
        },
        {
            "name": "Pipeline Value",
            "metric_type": MetricType.REVENUE,
            "calculation": "SUM(deals.value WHERE status IN ('qualification', 'proposal', 'negotiation'))",
            "unit": "$",
            "description": "Total value of open opportunities",
            "target_value": 2000000.0,
        },
        {
            "name": "Win Rate",
            "metric_type": MetricType.PERCENTAGE,
            "calculation": "COUNT(deals.id WHERE status='closed_won') / COUNT(deals.id WHERE status IN ('closed_won', 'closed_lost'))",
            "unit": "%",
            "description": "Percentage of deals closed won",
            "target_value": 30.0,
        },
        {
            "name": "Sales Cycle Duration",
            "metric_type": MetricType.TIME,
            "calculation": "AVG(EXTRACT(DAY FROM (deals.close_date - deals.created_at)))",
            "unit": "days",
            "description": "Average number of days to close a deal",
            "target_value": 45.0,
        },
        {
            "name": "Customer Count",
            "metric_type": MetricType.COUNT,
            "calculation": "COUNT(accounts.id WHERE status='active')",
            "unit": "accounts",
            "description": "Total active customer accounts",
            "target_value": 100.0,
        },
        {
            "name": "Net Retention Rate",
            "metric_type": MetricType.PERCENTAGE,
            "calculation": "(MRR_current + Expansion - Churn) / MRR_previous",
            "unit": "%",
            "description": "Monthly revenue retention including expansion",
            "target_value": 110.0,
        },
        {
            "name": "Churn Rate",
            "metric_type": MetricType.PERCENTAGE,
            "calculation": "COUNT(churned_customers) / COUNT(total_customers_start_month)",
            "unit": "%",
            "description": "Monthly customer churn rate",
            "target_value": 2.0,
        },
        {
            "name": "Lead Score Average",
            "metric_type": MetricType.RATIO,
            "calculation": "AVG(leads.score WHERE created_at >= NOW() - INTERVAL 30 DAY)",
            "unit": "score",
            "description": "Average lead quality score for new leads",
            "target_value": 75.0,
        },
    ]

    for metric_data in metrics_data:
        metric = AnalyticsMetric(
            id=f"metric_{uuid.uuid4().hex[:12]}",
            **metric_data,
        )
        AnalyticsEngine.register_metric(metric)

    print(f"✅ Created {len(metrics_data)} demo metrics")
    db.close()


def create_demo_dashboards():
    """Create demo dashboards."""
    db = SessionLocal()

    dashboard_configs = [
        {
            "name": "Executive Dashboard",
            "dashboard_type": DashboardType.EXECUTIVE,
            "description": "High-level business metrics for C-level executives",
            "owner": "admin",
        },
        {
            "name": "Sales Dashboard",
            "dashboard_type": DashboardType.SALES,
            "description": "Pipeline and deal metrics for sales team",
            "owner": "sales",
        },
        {
            "name": "CSM Dashboard",
            "dashboard_type": DashboardType.CSM,
            "description": "Customer health and retention metrics",
            "owner": "csm",
        },
        {
            "name": "Marketing Dashboard",
            "dashboard_type": DashboardType.MARKETING,
            "description": "Lead generation and campaign performance",
            "owner": "marketing",
        },
        {
            "name": "Operations Dashboard",
            "dashboard_type": DashboardType.OPERATIONS,
            "description": "System health and automation metrics",
            "owner": "ops",
        },
    ]

    for config in dashboard_configs:
        dashboard = AnalyticsEngine.create_dashboard(**config)

    print(f"✅ Created {len(dashboard_configs)} demo dashboards")
    db.close()


def record_demo_data_points():
    """Record sample data points for metrics."""
    db = SessionLocal()

    metrics = AnalyticsEngine.list_metrics()
    if not metrics:
        print("⚠️  No metrics available to record data points")
        return

    # Generate data points for the last 30 days
    data_points_created = 0
    now = datetime.now(timezone.utc)

    for metric in metrics:
        for day_offset in range(30):
            timestamp = now - timedelta(days=day_offset)

            value = demo_metric_sample_value(metric, day_offset)

            # Record data point with dimensions
            dimensions = ["enterprise", "mid-market", "smb"]
            dimension = dimensions[day_offset % len(dimensions)]

            AnalyticsEngine.record_data_point(
                metric_id=metric.id,
                value=max(0, value),
                dimension=dimension,
                metadata={
                    "region": ["North America", "EMEA", "APAC"][day_offset % 3],
                    "team": ["Sales", "Marketing", "CSM"][day_offset % 3],
                    "source": "demo",
                },
            )
            data_points_created += 1

    print(f"✅ Recorded {data_points_created} demo data points")
    db.close()


def create_demo_contacts():
    """Create sample contact data (if contact model exists)."""
    try:
        from revenue_os.models.contact import Contact, ContactStatus
        from revenue_os.database import SessionLocal

        db = SessionLocal()

        sample_contacts = [
            {
                "first_name": "Sarah",
                "last_name": "Johnson",
                "email": "sarah.johnson@acme.com",
                "phone": "+1-555-0101",
                "company": "Acme Corp",
                "title": "VP Sales",
                "status": ContactStatus.QUALIFIED,
            },
            {
                "first_name": "Michael",
                "last_name": "Chen",
                "email": "m.chen@techflow.io",
                "phone": "+1-555-0102",
                "company": "TechFlow",
                "title": "CRO",
                "status": ContactStatus.PROSPECT,
            },
            {
                "first_name": "Emma",
                "last_name": "Rodriguez",
                "email": "emma.r@innovate.com",
                "phone": "+1-555-0103",
                "company": "Innovate Ltd",
                "title": "Revenue Operations Director",
                "status": ContactStatus.QUALIFIED,
            },
            {
                "first_name": "James",
                "last_name": "Wilson",
                "email": "j.wilson@enterprise.com",
                "phone": "+1-555-0104",
                "company": "Enterprise Solutions",
                "title": "Chief Revenue Officer",
                "status": ContactStatus.CUSTOMER,
            },
            {
                "first_name": "Lisa",
                "last_name": "Kumar",
                "email": "lisa.kumar@startup.io",
                "phone": "+1-555-0105",
                "company": "StartupIO",
                "title": "Founder",
                "status": ContactStatus.PROSPECT,
            },
        ]

        for contact_data in sample_contacts:
            contact = Contact(**contact_data)
            db.add(contact)

        db.commit()
        print(f"✅ Created {len(sample_contacts)} demo contacts")
        db.close()
    except Exception as e:
        print(f"⚠️  Could not create demo contacts: {e}")


def main():
    """Initialize demo database."""
    print("\n🚀 Initializing Founder OS Demo Database\n")

    try:
        init_database()
        create_demo_metrics()
        create_demo_dashboards()
        record_demo_data_points()
        create_demo_contacts()

        print("\n✅ Demo database initialization complete!")
        print("\n📊 Demo Data Summary:")
        print(f"   - Metrics: {len(AnalyticsEngine.list_metrics())}")
        print(f"   - Dashboards: {len(AnalyticsEngine.list_dashboards())}")
        print(f"   - Data Points: {len(AnalyticsEngine._data_points)}")
        print("\n🎯 Next Steps:")
        print("   1. Access API: http://localhost:8000")
        print("   2. View Docs: http://localhost:8000/docs")
        print("   3. Test Endpoints: See DEMO_SETUP.md for examples")

    except Exception as e:
        print(f"\n❌ Error initializing demo database: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
