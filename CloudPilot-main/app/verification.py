"""V3 verification engine: compare recorded pre-deployment predictions to outcomes."""
from __future__ import annotations

from statistics import fmean

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .models import AuditLog, ChangeAnalysis, VerificationResult


def _percentage_error(predicted: float, actual: float) -> float:
    """Relative error using a $1 floor so zero-cost predictions remain comparable."""
    return round(abs(actual - predicted) / max(abs(predicted), 1.0) * 100, 2)


def _dependency_scores(predicted: set[str], observed: set[str]) -> tuple[float, float, float]:
    if not predicted and not observed:
        return 100.0, 100.0, 100.0
    overlap = len(predicted & observed)
    precision = round(overlap / len(predicted) * 100, 2) if predicted else 0.0
    recall = round(overlap / len(observed) * 100, 2) if observed else 0.0
    f1 = round(2 * precision * recall / (precision + recall), 2) if precision + recall else 0.0
    return precision, recall, f1


def _health_status(telemetry: dict) -> str:
    latency_before, latency_after = telemetry.get("latency_ms_before"), telemetry.get("latency_ms_after")
    errors_before, errors_after = telemetry.get("error_rate_before"), telemetry.get("error_rate_after")
    availability_before, availability_after = telemetry.get("availability_before"), telemetry.get("availability_after")
    critical = (
        latency_before is not None and latency_after is not None and latency_before > 0 and latency_after / latency_before >= 2
    ) or (
        errors_after is not None and errors_after >= 0.05
    ) or (
        availability_after is not None and availability_after < 0.99
    )
    degraded = (
        latency_before is not None and latency_after is not None and latency_before > 0 and latency_after / latency_before > 1.2
    ) or (
        errors_before is not None and errors_after is not None and errors_after - errors_before > 0.01
    ) or (
        availability_before is not None and availability_after is not None and availability_before - availability_after > 0.005
    )
    observed = any(value is not None for value in telemetry.values())
    return "CRITICAL" if critical else "DEGRADED" if degraded else "HEALTHY" if observed else "NOT_OBSERVED"


def _risk_assessment(predicted_score: int | float, health_status: str) -> str:
    high_risk = predicted_score >= 60
    if health_status == "NOT_OBSERVED":
        return "TELEMETRY_PENDING"
    if health_status in {"DEGRADED", "CRITICAL"}:
        return "RISK_CONFIRMED" if high_risk else "RISK_UNDERESTIMATED"
    return "RISK_CONSERVATIVE" if high_risk else "RISK_CONFIRMED"


def _resource_types(record: ChangeAnalysis) -> set[str]:
    return {change["resource_type"] for change in record.report["changes"]}


def confidence_details_for_types(db: Session, resource_types: set[str]) -> dict:
    """Derive historical confidence from verified cost error, not a statistical claim."""
    historical_errors: list[float] = []
    for verification in db.scalars(select(VerificationResult)):
        analysis = db.get(ChangeAnalysis, verification.analysis_id)
        if analysis and resource_types & _resource_types(analysis):
            historical_errors.append(verification.cost_error_percent)
    # A documented neutral prior is used until this resource category has history.
    if not historical_errors:
        return {"score": 75.0, "observations": 0, "mean_cost_error_percent": None}
    mean_error = round(fmean(historical_errors), 2)
    return {"score": round(max(0.0, 100.0 - mean_error), 2), "observations": len(historical_errors), "mean_cost_error_percent": mean_error}


def confidence_for_types(db: Session, resource_types: set[str]) -> float:
    """Compatibility helper for callers that only need the score."""
    return confidence_details_for_types(db, resource_types)["score"]


def verify_analysis(db: Session, record: ChangeAnalysis, submission: dict) -> VerificationResult:
    if db.scalar(select(VerificationResult).where(VerificationResult.analysis_id == record.id)):
        raise ValueError("This analysis already has a verification result. Create a new analysis for a new deployment.")
    deployment_identifier = submission.get("deployment_identifier")
    if deployment_identifier and db.scalar(select(VerificationResult).where(VerificationResult.deployment_identifier == deployment_identifier)):
        raise ValueError("This deployment identifier is already associated with another verification.")
    if deployment_identifier:
        record.deployment_id = deployment_identifier
    predicted_cost = record.monthly_cost_delta
    actual_cost = submission["actual_monthly_cost_delta"]
    predicted_dependencies = {node["id"] for node in record.report["blast_radius"]["impacted_nodes"]}
    observed_dependencies = {item.strip() for item in submission["observed_affected_resources"] if item.strip()}
    precision, recall, f1 = _dependency_scores(predicted_dependencies, observed_dependencies)
    telemetry = submission["telemetry"]
    health = _health_status(telemetry)
    resource_types = _resource_types(record)
    confidence_before = confidence_details_for_types(db, resource_types)
    cost_error_percent = _percentage_error(predicted_cost, actual_cost)
    verification = VerificationResult(
        analysis_id=record.id, deployment_identifier=submission.get("deployment_identifier"),
        actual_monthly_cost_delta=actual_cost,
        observed_affected_resources=sorted(observed_dependencies), telemetry=telemetry,
        cost_error_amount=round(actual_cost - predicted_cost, 2), cost_error_percent=cost_error_percent,
        dependency_precision=precision, dependency_recall=recall, dependency_f1=f1,
        health_status=health, risk_assessment=_risk_assessment(record.report["risk"]["score"], health),
        confidence_before=confidence_before["score"], confidence_after=0, actor=submission["actor"],
    )
    db.add(verification)
    db.flush()
    verification.confidence_after = confidence_for_types(db, resource_types)
    db.add(AuditLog(analysis_id=record.id, actor=submission["actor"], event="prediction_verified", details={
        "verification_id": verification.id, "cost_error_percent": verification.cost_error_percent,
        "health_status": verification.health_status, "risk_assessment": verification.risk_assessment,
    }))
    db.commit()
    db.refresh(verification)
    return verification


def response_for_verification(result: VerificationResult, record: ChangeAnalysis) -> dict:
    result_session = object_session(result)
    confidence = confidence_details_for_types(result_session, _resource_types(record)) if result_session else {"observations": 0, "mean_cost_error_percent": None}
    return {"id": result.id, "analysis_id": result.analysis_id, "deployment_identifier": result.deployment_identifier, "predicted_monthly_cost_delta": record.monthly_cost_delta,
            "actual_monthly_cost_delta": result.actual_monthly_cost_delta, "cost_error_amount": result.cost_error_amount,
            "cost_error_percent": result.cost_error_percent, "dependency_precision": result.dependency_precision,
            "dependency_recall": result.dependency_recall, "dependency_f1": result.dependency_f1,
            "health_status": result.health_status, "risk_assessment": result.risk_assessment,
            "confidence_before": result.confidence_before, "confidence_after": result.confidence_after,
            "confidence_observations": confidence["observations"], "historical_mean_cost_error_percent": confidence["mean_cost_error_percent"],
            "created_at": result.created_at}


def verification_history(db: Session) -> list[dict]:
    results = db.scalars(select(VerificationResult).order_by(VerificationResult.created_at.desc()).limit(50))
    return [response_for_verification(result, db.get(ChangeAnalysis, result.analysis_id)) for result in results]
