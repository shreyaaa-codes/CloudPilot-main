"""Persistent records for CloudPilot's AWS change-intelligence workflow."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Team(Base):
    """Business owner of services, budgets, and maintenance windows."""
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    services: Mapped[list["Service"]] = relationship(back_populates="team")


class Budget(Base):
    """A team's monthly budget for an optional deployment environment."""
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("team_id", "environment", name="uq_budget_team_environment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    environment: Mapped[str] = mapped_column(String(32), default="production")
    remaining_monthly_budget: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    team: Mapped[Team] = relationship()


class ServiceCriticality(Base):
    """Named business criticality level used by services and risk scoring."""
    __tablename__ = "service_criticalities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    risk_points: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)


class Service(Base):
    """A deployable business service owned by a team."""
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    environment: Mapped[str] = mapped_column(String(32), default="production")
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    criticality_id: Mapped[int | None] = mapped_column(ForeignKey("service_criticalities.id"), nullable=True)
    team: Mapped[Team] = relationship(back_populates="services")
    criticality: Mapped[ServiceCriticality | None] = relationship()


class ServiceResource(Base):
    """Maps a Terraform address or discovered resource identifier to a service.

    It deliberately uses an external identifier instead of a foreign key so a
    plan can be evaluated before an AWS resource has been discovered.
    """
    __tablename__ = "service_resources"
    __table_args__ = (UniqueConstraint("service_id", "resource_external_id", name="uq_service_resource"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    resource_external_id: Mapped[str] = mapped_column(String(512), index=True)
    service: Mapped[Service] = relationship()


class MaintenanceWindow(Base):
    """Time-bounded approved maintenance period for a team or service."""
    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"), nullable=True, index=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Policy(Base):
    """Persisted policy configuration consumed by the decision engine."""
    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChangeAnalysis(Base):
    """One Terraform plan submitted to CloudPilot and its explained decision."""
    __tablename__ = "change_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    project: Mapped[str] = mapped_column(String(120), default="default")
    pull_request: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    environment: Mapped[str] = mapped_column(String(32), default="development")
    team: Mapped[str] = mapped_column(String(120), default="unassigned")
    decision: Mapped[str] = mapped_column(String(16), index=True)
    current_monthly_cost: Mapped[float] = mapped_column(Float, default=0)
    proposed_monthly_cost: Mapped[float] = mapped_column(Float, default=0)
    monthly_cost_delta: Mapped[float] = mapped_column(Float, default=0)
    plan: Mapped[dict] = mapped_column(JSON)
    context: Mapped[dict] = mapped_column(JSON)
    report: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only trace of a CloudPilot decision."""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResourceNode(Base):
    """A live AWS asset represented as a node in V2's dependency graph."""
    __tablename__ = "resource_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    region: Mapped[str] = mapped_column(String(32))
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terraform_address: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)


class DependencyEdge(Base):
    """A directed relation: source supports or leads to target."""
    __tablename__ = "dependency_edges"
    __table_args__ = (UniqueConstraint("source_external_id", "target_external_id", "edge_type", name="uq_dependency_edge"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_external_id: Mapped[str] = mapped_column(String(512), index=True)
    target_external_id: Mapped[str] = mapped_column(String(512), index=True)
    edge_type: Mapped[str] = mapped_column(String(64))
    inferred_from: Mapped[str] = mapped_column(String(64), default="aws_metadata")
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TopologySync(Base):
    """One read-only AWS topology synchronization, retained for freshness evidence."""
    __tablename__ = "topology_syncs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), default="unknown")
    region: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    active_nodes: Mapped[int] = mapped_column(Integer, default=0)
    active_edges: Mapped[int] = mapped_column(Integer, default=0)
    stale_nodes_marked: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerificationResult(Base):
    """V3's post-deployment comparison of an analysis prediction and observed outcome."""
    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    deployment_identifier: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    actual_monthly_cost_delta: Mapped[float] = mapped_column(Float)
    observed_affected_resources: Mapped[list] = mapped_column(JSON, default=list)
    telemetry: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_error_amount: Mapped[float] = mapped_column(Float, default=0)
    cost_error_percent: Mapped[float] = mapped_column(Float, default=0)
    dependency_precision: Mapped[float] = mapped_column(Float, default=0)
    dependency_recall: Mapped[float] = mapped_column(Float, default=0)
    dependency_f1: Mapped[float] = mapped_column(Float, default=0)
    health_status: Mapped[str] = mapped_column(String(32))
    risk_assessment: Mapped[str] = mapped_column(String(48))
    confidence_before: Mapped[float] = mapped_column(Float)
    confidence_after: Mapped[float] = mapped_column(Float)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
