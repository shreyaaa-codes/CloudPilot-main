"""Application service that persists a pure-engine analysis and audit entry."""
from sqlalchemy.orm import Session

from .business_context import resolve_business_context
from .engines import analyze, normalize_plan
from .live_topology import DatabaseTopologySource
from .graph import FixtureTopologySource
from .models import AuditLog, ChangeAnalysis
from .redaction import redact_plan, redact_report


def submit_plan(db: Session, plan: dict, context: dict, provided_fields: set[str] | None = None) -> ChangeAnalysis:
    """Persist an analysis after resolving optional database-backed business context."""
    resolved_context = resolve_business_context(
        db, {**context, "resource_addresses": [change["address"] for change in normalize_plan(plan)]}, provided_fields
    )
    resolved_context.pop("resource_addresses", None)
    use_fixture = context.get("use_fixture_topology", False)
    topology_source = FixtureTopologySource() if use_fixture else DatabaseTopologySource(db)
    report = redact_report(analyze(plan, resolved_context, topology_source=topology_source), plan)
    record = ChangeAnalysis(
        project=resolved_context["project"], pull_request=resolved_context.get("pull_request"), environment=resolved_context["environment"],
        team=resolved_context["team"], decision=report["decision"], current_monthly_cost=report["current_monthly_cost"],
        proposed_monthly_cost=report["proposed_monthly_cost"], monthly_cost_delta=report["monthly_cost_delta"],
        plan=redact_plan(plan), context=resolved_context, report=report,
    )
    db.add(record)
    db.flush()
    db.add(AuditLog(analysis_id=record.id, actor=resolved_context["actor"], event="analysis_created",
                    details={"decision": record.decision, "findings": [item["id"] for item in report["findings"]]}))
    db.commit()
    db.refresh(record)
    return record


def response_for(record: ChangeAnalysis) -> dict:
    return {"id": record.id, "deployment_id": record.deployment_id, "decision": record.decision, "project": record.project, "pull_request": record.pull_request,
            "environment": record.environment, "current_monthly_cost": record.current_monthly_cost,
            "proposed_monthly_cost": record.proposed_monthly_cost, "monthly_cost_delta": record.monthly_cost_delta,
            "changes": record.report["changes"], "findings": record.report["findings"],
            "risk": record.report["risk"], "blast_radius": record.report["blast_radius"],
            "business_context": {key: value for key, value in record.context.items() if key not in {"policy_catalogue", "service_criticality"}},
            "pricing": record.report.get("pricing", {"source": "repository static USD catalogue", "kind": "estimate"}),
            "explanation": record.report["explanation"], "created_at": record.created_at}


def github_comment(record: ChangeAnalysis) -> str:
    report = record.report
    icon = {"ALLOW": "🟢", "WARN": "🟡", "BLOCK": "🔴"}[record.decision]
    findings = "\n".join(f"- **{item['id']}**: {item['message']}" for item in report["findings"]) or "- No policy violations"
    return (f"## ☁️ CloudPilot Change Analysis\n\n"
            f"**Decision:** {icon} **{record.decision}**\n\n"
            f"| Cost estimate | Value |\n|---|---:|\n| Current | ${record.current_monthly_cost:.2f}/month |\n"
            f"| Proposed | ${record.proposed_monthly_cost:.2f}/month |\n| Delta | ${record.monthly_cost_delta:+.2f}/month |\n\n"
            f"**Composite risk:** {report['risk']['score']}/100 — {report['risk']['label']}  \n"
            f"**Blast radius:** {report['blast_radius']['direct_dependencies']} direct, {report['blast_radius']['indirect_dependencies']} indirect, {report['blast_radius']['tier1_services']} Tier-1 services  \n\n"
            f"### Policy findings\n{findings}\n\n### Why?\n{report['explanation']}\n\n"
            f"_CloudPilot uses a version-controlled static pricing catalogue for supported resources; this is an estimate, not AWS billing data._")
