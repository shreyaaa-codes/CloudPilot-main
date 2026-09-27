"""Business-context persistence and API compatibility tests."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.business_context import resolve_business_context
from app.database import Base, SessionLocal, engine
from app.engines import analyze
from app.graph import FixtureTopologySource, TopologySourceRequiredError
from app.models import MaintenanceWindow, Policy, Service, Team


def reset_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_demo_context_is_seeded_and_resolves_checkout_business_data():
    reset_database()
    db = SessionLocal()
    try:
        context = resolve_business_context(db, {
            "team": "unassigned", "environment": "production",
            "resource_addresses": ["aws_db_instance.checkout"],
        }, {"environment"})

        assert context["team"] == "Payments"
        assert context["remaining_budget"] == 100.0
        criticality = context["service_criticality"]["aws_db_instance.checkout"]
        assert criticality[0]["criticality"] == "Tier1"
        assert db.scalar(select(Service).where(Service.slug == "checkout-api")) is not None
    finally:
        db.close()


def test_request_budget_and_maintenance_fields_override_persisted_context():
    reset_database()
    db = SessionLocal()
    try:
        resolved = resolve_business_context(db, {
            "team": "Payments", "environment": "production", "remaining_budget": 7,
            "maintenance_window_active": False, "resource_addresses": ["aws_db_instance.checkout"],
        }, {"team", "environment", "remaining_budget", "maintenance_window_active"})

        assert resolved["remaining_budget"] == 7
        assert resolved["maintenance_window_active"] is False
    finally:
        db.close()


def test_active_persisted_maintenance_window_is_resolved_when_not_submitted():
    reset_database()
    db = SessionLocal()
    try:
        resolved = resolve_business_context(db, {
            "team": "Payments", "environment": "production", "resource_addresses": ["aws_db_instance.checkout"],
        }, {"team", "environment"})
        payments = db.scalar(select(Team).where(Team.name == "Payments"))
        now = datetime.now(timezone.utc)
        db.add(MaintenanceWindow(team_id=payments.id, environment="production", starts_at=now - timedelta(minutes=1), ends_at=now + timedelta(minutes=1)))
        db.flush()

        resolved = resolve_business_context(db, {
            "team": "Payments", "environment": "production", "resource_addresses": ["aws_db_instance.checkout"],
        }, {"team", "environment"})

        assert resolved["maintenance_window_active"] is True
    finally:
        db.close()


def test_persisted_policy_configuration_is_used_instead_of_json_defaults():
    reset_database()
    db = SessionLocal()
    try:
        resolve_business_context(db, {"team": "Payments", "environment": "production"}, {"team", "environment"})
        policy = db.scalar(select(Policy).where(Policy.key == "cost_limits"))
        policy.configuration = {"warn_above": 3, "block_above": 5}
        db.flush()

        resolved = resolve_business_context(db, {"team": "Payments", "environment": "production"}, {"team", "environment"})
        assert resolved["policy_catalogue"]["cost_limits"] == {"warn_above": 3, "block_above": 5}
        report = analyze({"resource_changes": [{
            "address": "aws_ebs_volume.data", "type": "aws_ebs_volume",
            "change": {"actions": ["update"], "before": {"size": 10, "type": "gp3"}, "after": {"size": 100, "type": "gp3"}},
        }]}, resolved, topology_source=FixtureTopologySource({"nodes": [], "edges": []}))
        assert "BUDGET-002" in {finding["id"] for finding in report["findings"]}
    finally:
        db.close()


def test_analysis_requires_an_explicit_topology_source():
    try:
        analyze({"resource_changes": []}, {"environment": "development"})
    except TopologySourceRequiredError as exc:
        assert "topology source is required" in str(exc).lower()
    else:
        raise AssertionError("analysis must not silently calculate a zero blast radius without topology")
