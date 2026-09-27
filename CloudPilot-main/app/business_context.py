"""Database initialization, deterministic demo data, and policy context lookup."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .database import Base, engine
from .models import Budget, MaintenanceWindow, Policy, Service, ServiceCriticality, ServiceResource, Team, ResourceNode, DependencyEdge


DEFAULT_COST_LIMITS = {"warn_above": 100, "block_above": 250}


def initialize_database() -> None:
    """Apply the additive schema initialization and optional local demo context."""
    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()
    if settings.seed_demo_data:
        with Session(bind=engine) as db:
            ensure_demo_data(db)
            db.commit()


def _apply_additive_migrations() -> None:
    """Keep existing local SQLite databases compatible with additive V3 fields."""
    inspector = inspect(engine)
    if "resource_nodes" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("resource_nodes")}
    statements: list[str] = []
    if "is_active" not in columns:
        statements.append("ALTER TABLE resource_nodes ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
    if "last_seen_at" not in columns:
        statements.append("ALTER TABLE resource_nodes ADD COLUMN last_seen_at TIMESTAMP WITH TIME ZONE")
    if "terraform_address" not in columns:
        statements.append("ALTER TABLE resource_nodes ADD COLUMN terraform_address VARCHAR(512)")
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("UPDATE resource_nodes SET is_active = TRUE WHERE is_active IS NULL"))
        analysis_tables = set(inspect(engine).get_table_names())
        if "change_analyses" in analysis_tables:
            analysis_columns = {column["name"] for column in inspect(engine).get_columns("change_analyses")}
            if "deployment_id" not in analysis_columns:
                connection.execute(text("ALTER TABLE change_analyses ADD COLUMN deployment_id VARCHAR(160)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_change_analyses_deployment_id ON change_analyses (deployment_id)"))
        verification_tables = set(inspect(engine).get_table_names())
        if "verification_results" in verification_tables:
            verification_columns = {column["name"] for column in inspect(engine).get_columns("verification_results")}
            if "deployment_identifier" not in verification_columns:
                connection.execute(text("ALTER TABLE verification_results ADD COLUMN deployment_identifier VARCHAR(160)"))
            if "change_analyses" in analysis_tables:
                connection.execute(text(
                    "UPDATE change_analyses SET deployment_id = "
                    "(SELECT deployment_identifier FROM verification_results "
                    "WHERE verification_results.analysis_id = change_analyses.id) "
                    "WHERE deployment_id IS NULL AND EXISTS "
                    "(SELECT 1 FROM verification_results "
                    "WHERE verification_results.analysis_id = change_analyses.id "
                    "AND deployment_identifier IS NOT NULL)"
                ))


def ensure_demo_data(db: Session) -> None:
    """Insert only missing records, making local startup and test resets repeatable."""
    payments = db.scalar(select(Team).where(Team.slug == "payments"))
    if payments is None:
        payments = Team(slug="payments", name="Payments")
        db.add(payments)
        db.flush()
    if db.scalar(select(Team).where(Team.slug == "platform")) is None:
        db.add(Team(slug="platform", name="Platform"))

    tier1 = db.scalar(select(ServiceCriticality).where(ServiceCriticality.name == "Tier1"))
    if tier1 is None:
        tier1 = ServiceCriticality(name="Tier1", risk_points=25, description="Business-critical customer path")
        db.add(tier1)
        db.flush()
    if db.scalar(select(ServiceCriticality).where(ServiceCriticality.name == "Tier2")) is None:
        db.add(ServiceCriticality(name="Tier2", risk_points=12, description="Important supporting service"))
    if db.scalar(select(ServiceCriticality).where(ServiceCriticality.name == "Tier3")) is None:
        db.add(ServiceCriticality(name="Tier3", risk_points=0, description="Standard business service"))

    checkout = db.scalar(select(Service).where(Service.slug == "checkout-api"))
    if checkout is None:
        checkout = Service(slug="checkout-api", name="checkout-api", environment="production", team_id=payments.id, criticality_id=tier1.id)
        db.add(checkout)
        db.flush()
    if db.scalar(select(ServiceResource).where(ServiceResource.service_id == checkout.id, ServiceResource.resource_external_id == "aws_db_instance.checkout")) is None:
        db.add(ServiceResource(service_id=checkout.id, resource_external_id="aws_db_instance.checkout"))

    if db.scalar(select(Budget).where(Budget.team_id == payments.id, Budget.environment == "production")) is None:
        db.add(Budget(team_id=payments.id, environment="production", remaining_monthly_budget=100.0))
    if db.scalar(select(Policy).where(Policy.key == "cost_limits")) is None:
        db.add(Policy(key="cost_limits", name="Monthly cost limits", configuration=DEFAULT_COST_LIMITS))

    # SessionLocal intentionally disables autoflush; callers immediately query
    # the seeded mapping while evaluating the same request.
    db.flush()


def resolve_business_context(db: Session, context: dict[str, Any], provided_fields: set[str] | None = None) -> dict[str, Any]:
    """Resolve persisted context, allowing explicitly submitted fields to win."""
    if settings.seed_demo_data:
        ensure_demo_data(db)
    fields = provided_fields if provided_fields is not None else set(context)
    team = db.scalar(select(Team).where(or_(Team.name.ilike(context.get("team", "")), Team.slug.ilike(context.get("team", "")))))
    addresses = context.get("resource_addresses", [])
    services = list(db.scalars(
        select(Service).join(ServiceResource).options(joinedload(Service.criticality)).where(ServiceResource.resource_external_id.in_(addresses))
    )) if addresses else []
    if team is None and services:
        team = db.get(Team, services[0].team_id)

    resolved = dict(context)
    if team is not None:
        resolved["team"] = team.name
        resolved["team_id"] = team.id
    if "remaining_budget" not in fields and team is not None:
        budget = db.scalar(select(Budget).where(Budget.team_id == team.id, Budget.environment == context.get("environment", "development")))
        if budget is not None:
            resolved["remaining_budget"] = budget.remaining_monthly_budget
    if "maintenance_window_active" not in fields:
        now = datetime.now(timezone.utc)
        service_ids = [service.id for service in services]
        window_filters = [MaintenanceWindow.team_id == (team.id if team else -1)]
        if service_ids:
            window_filters.append(MaintenanceWindow.service_id.in_(service_ids))
        active_window = db.scalar(select(MaintenanceWindow).where(
            MaintenanceWindow.enabled.is_(True), MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at >= now,
            or_(*window_filters),
            or_(MaintenanceWindow.environment.is_(None), MaintenanceWindow.environment == context.get("environment")),
        ))
        resolved["maintenance_window_active"] = active_window is not None
        resolved["maintenance_window"] = ({"id": active_window.id, "starts_at": active_window.starts_at.isoformat(),
                                            "ends_at": active_window.ends_at.isoformat(), "source": "database"}
                                           if active_window else None)
    else:
        resolved["maintenance_window"] = {"source": "request"} if context.get("maintenance_window_active") else None
    limits = db.scalar(select(Policy).where(Policy.key == "cost_limits", Policy.enabled.is_(True)))
    resolved["policy_catalogue"] = {"cost_limits": (limits.configuration if limits else DEFAULT_COST_LIMITS)}
    service_criticality: dict[str, list[dict[str, Any]]] = {}
    for service in services:
        if service.criticality is None:
            continue
        for resource in db.scalars(select(ServiceResource).where(ServiceResource.service_id == service.id)):
            service_criticality.setdefault(resource.resource_external_id, []).append({
                "service_id": str(service.id),
                "criticality": service.criticality.name,
                "risk_points": service.criticality.risk_points,
            })
    resolved["service_criticality"] = service_criticality
    return resolved
