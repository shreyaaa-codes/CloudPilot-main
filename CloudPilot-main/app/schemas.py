"""HTTP request and response contracts; FastAPI documents these automatically."""
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AnalysisContext(BaseModel):
    project: str = Field(default="default", min_length=1, max_length=120)
    pull_request: str | None = Field(default=None, max_length=64)
    environment: Literal["development", "staging", "production"] = "development"
    team: str = Field(default="unassigned", min_length=1, max_length=120)
    remaining_budget: float | None = Field(default=None, ge=0)
    maintenance_window_active: bool = False
    use_fixture_topology: bool = False
    actor: str = Field(default="cloudpilot-reviewer", min_length=1, max_length=128)


class PlanSubmission(BaseModel):
    """The parsed JSON produced by `terraform show -json tfplan`."""
    plan: dict[str, Any]
    context: AnalysisContext = Field(default_factory=AnalysisContext)


class ChangeSummary(BaseModel):
    address: str
    resource_type: str
    action: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    changed_attributes: list[str]
    current_monthly_cost: float
    proposed_monthly_cost: float
    monthly_delta: float


class AnalysisResponse(BaseModel):
    id: int
    deployment_id: str | None = None
    decision: str
    project: str
    pull_request: str | None
    environment: str
    current_monthly_cost: float
    proposed_monthly_cost: float
    monthly_cost_delta: float
    changes: list[ChangeSummary]
    findings: list[dict]
    risk: dict
    blast_radius: dict
    business_context: dict = Field(default_factory=dict)
    pricing: dict = Field(default_factory=dict)
    explanation: str
    created_at: datetime


class DashboardSummary(BaseModel):
    total_changes: int
    allowed: int
    warnings: int
    blocked: int
    monthly_cost_delta: float


class AuditEntry(BaseModel):
    event: str
    actor: str
    details: dict
    created_at: datetime


class TelemetryObservation(BaseModel):
    """Observed post-deployment health values; all fields are optional by design."""
    latency_ms_before: float | None = Field(default=None, ge=0)
    latency_ms_after: float | None = Field(default=None, ge=0)
    error_rate_before: float | None = Field(default=None, ge=0, le=1)
    error_rate_after: float | None = Field(default=None, ge=0, le=1)
    availability_before: float | None = Field(default=None, ge=0, le=1)
    availability_after: float | None = Field(default=None, ge=0, le=1)
    cloudwatch_cpu: dict[str, Any] | None = None
    cost_explorer: dict[str, Any] | None = None


class VerificationSubmission(BaseModel):
    deployment_identifier: str | None = Field(default=None, max_length=160)
    actual_monthly_cost_delta: float
    observed_affected_resources: list[str] = Field(default_factory=list)
    telemetry: TelemetryObservation = Field(default_factory=TelemetryObservation)
    actor: str = Field(default="post-deployment-reviewer", min_length=1, max_length=128)


class VerificationResponse(BaseModel):
    id: int
    analysis_id: int
    deployment_identifier: str | None
    predicted_monthly_cost_delta: float
    actual_monthly_cost_delta: float
    cost_error_amount: float
    cost_error_percent: float
    dependency_precision: float
    dependency_recall: float
    dependency_f1: float
    health_status: str
    risk_assessment: str
    confidence_before: float
    confidence_after: float
    confidence_observations: int
    historical_mean_cost_error_percent: float | None
    created_at: datetime


class Ec2CpuTelemetryRequest(BaseModel):
    instance_id: str = Field(min_length=3, max_length=64)
    window_minutes: int = Field(default=60, ge=5, le=1440)


class CostExplorerTelemetryRequest(BaseModel):
    start_date: date
    end_date: date
    service: str | None = Field(default=None, max_length=128)
