"""Pure decision-engine logic: Terraform plan -> cost, policy, impact, and decision."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from .graph import TopologySource, blast_radius

SUPPORTED_TYPES = {
    "aws_instance", "aws_db_instance", "aws_ebs_volume",
    "aws_nat_gateway", "aws_eip", "aws_lb", "aws_s3_bucket",
}
# Static us-east-1 list-price estimates use a 730-hour month. NAT and load
# balancer estimates omit usage charges; S3 assumes 10 GB of Standard storage.
HOURS_PER_MONTH = 730
PRICE_CATALOGUE = {
    "aws_instance": {"t3.micro": 7.59, "t3.small": 15.18, "t3.medium": 30.37, "m6i.large": 70.08, "m6i.xlarge": 140.16},
    "aws_db_instance": {"db.t3.micro": 15.33, "db.t3.small": 30.66, "db.t3.medium": 61.32, "db.r6g.large": 138.70, "db.r6g.2xlarge": 554.80},
    "ebs_gb_month": {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125},
    "aws_nat_gateway": {"hourly": 0.045},
    "aws_eip": {"hourly": 0.005},
    "aws_lb": {"hourly": {"application": 0.0225, "network": 0.0225, "gateway": 0.0125}},
    "aws_s3_bucket": {"standard_gb_month": 0.023, "assumed_storage_gb": 10},
}
POLICY_PATH = Path(__file__).parent.parent / "policies" / "default.json"


def action_name(actions: Any) -> str:
    """Translate Terraform's action array into CloudPilot's supported action names."""
    if not isinstance(actions, list):
        return "unknown"
    if actions == ["create"]: return "create"
    if actions == ["update"]: return "update"
    if actions == ["delete"]: return "delete"
    if len(actions) == 2 and set(actions) == {"create", "delete"}: return "replace"
    return "unknown"


def changed_attributes(before: Any, after: Any) -> list[str]:
    """Return stable leaf paths for changed Terraform attributes, including nested values."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    missing = object()

    def diff(before_value: Any, after_value: Any, path: str) -> list[str]:
        if isinstance(before_value, dict) and isinstance(after_value, dict):
            paths: list[str] = []
            for key in sorted(set(before_value) | set(after_value)):
                child_path = f"{path}.{key}" if path else str(key)
                paths.extend(diff(before_value.get(key, missing), after_value.get(key, missing), child_path))
            return paths
        if isinstance(before_value, list) and isinstance(after_value, list):
            paths: list[str] = []
            for index in range(max(len(before_value), len(after_value))):
                child_path = f"{path}[{index}]"
                paths.extend(diff(
                    before_value[index] if index < len(before_value) else missing,
                    after_value[index] if index < len(after_value) else missing,
                    child_path,
                ))
            return paths
        return [path] if before_value != after_value else []

    return diff(before, after, "")


def resource_monthly_cost(resource_type: str, values: Any) -> float:
    """Estimate one supported resource from values emitted by terraform show -json."""
    if not isinstance(values, dict):
        return 0.0
    if resource_type in {"aws_instance", "aws_db_instance"}:
        return float(PRICE_CATALOGUE[resource_type].get(values.get("instance_type") or values.get("instance_class"), 0))
    if resource_type == "aws_ebs_volume":
        try:
            size = float(values.get("size", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return size * PRICE_CATALOGUE["ebs_gb_month"].get(values.get("type", "gp3"), 0)
    if resource_type in {"aws_nat_gateway", "aws_eip"}:
        return PRICE_CATALOGUE[resource_type]["hourly"] * HOURS_PER_MONTH
    if resource_type == "aws_lb":
        hourly_rates = PRICE_CATALOGUE["aws_lb"]["hourly"]
        load_balancer_type = values.get("load_balancer_type", "application")
        return hourly_rates.get(load_balancer_type, hourly_rates["application"]) * HOURS_PER_MONTH
    if resource_type == "aws_s3_bucket":
        bucket_pricing = PRICE_CATALOGUE["aws_s3_bucket"]
        return bucket_pricing["standard_gb_month"] * bucket_pricing["assumed_storage_gb"]
    return 0.0


def normalize_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract supported Terraform changes; skip no-ops, reads, and malformed entries."""
    if not isinstance(plan, dict):
        return []
    raw_changes = plan.get("resource_changes", [])
    if not isinstance(raw_changes, list):
        return []
    result = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            continue
        # Terraform resource_changes normally contains managed resources. Keeping
        # this explicit prevents a data-resource payload from being costed if it
        # is malformed or supplied by a non-standard producer.
        if raw.get("mode", "managed") != "managed":
            continue
        change = raw.get("change", {})
        if not isinstance(change, dict):
            continue
        resource_type = raw.get("type", "")
        action = action_name(change.get("actions", []))
        if action == "unknown" or resource_type not in SUPPORTED_TYPES:
            continue
        before = change.get("before") if isinstance(change.get("before"), dict) else {}
        after = change.get("after") if isinstance(change.get("after"), dict) else {}
        current, proposed = resource_monthly_cost(resource_type, before), resource_monthly_cost(resource_type, after)
        if action == "create": current = 0.0
        if action == "delete": proposed = 0.0
        address = raw.get("address") if isinstance(raw.get("address"), str) else resource_type
        result.append({"address": address, "resource_type": resource_type, "action": action,
                       "before": before, "after": after,
                       "changed_attributes": changed_attributes(before, after),
                       "current_monthly_cost": round(current, 2), "proposed_monthly_cost": round(proposed, 2),
                       "monthly_delta": round(proposed - current, 2)})
    return result


def unsupported_resource_findings(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Report actionable managed resources that the cost catalogue cannot estimate."""
    if not isinstance(plan, dict):
        return []
    raw_changes = plan.get("resource_changes", [])
    if not isinstance(raw_changes, list):
        return []
    findings = []
    for raw in raw_changes:
        if not isinstance(raw, dict) or raw.get("mode", "managed") != "managed":
            continue
        change = raw.get("change", {})
        if not isinstance(change, dict):
            continue
        resource_type = raw.get("type")
        if not isinstance(resource_type, str) or not resource_type or resource_type in SUPPORTED_TYPES:
            continue
        action = action_name(change.get("actions", []))
        if action == "unknown":
            continue
        address = raw.get("address") if isinstance(raw.get("address"), str) else resource_type
        findings.append({
            "id": "UNSUPPORTED_RESOURCE", "code": "UNSUPPORTED_RESOURCE", "severity": "warn",
            "address": address, "resource_type": resource_type, "action": action,
            "message": f"Cost estimation is not supported for Terraform resource type {resource_type} at {address} (action: {action}); manual review is required.",
        })
    return findings


def load_policies() -> dict[str, Any]:
    with POLICY_PATH.open() as file: return json.load(file)


def tags_for(change: dict[str, Any]) -> dict[str, str]:
    values = change["after"] if change["action"] != "delete" else change["before"]
    return {str(key): str(value) for key, value in (values.get("tags") or {}).items()}


def evaluate_policies(changes: list[dict[str, Any]], context: dict[str, Any], catalogue: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    delta, limits = sum(change["monthly_delta"] for change in changes), catalogue["cost_limits"]
    if delta > limits["block_above"]:
        findings.append({"id": "BUDGET-002", "severity": "block", "message": f"Projected monthly cost increase ${delta:.2f} exceeds blocking limit ${limits['block_above']:.2f}."})
    elif delta > limits["warn_above"]:
        findings.append({"id": "BUDGET-001", "severity": "warn", "message": f"Projected monthly cost increase ${delta:.2f} exceeds warning limit ${limits['warn_above']:.2f}."})
    if context.get("remaining_budget") is not None and delta > context["remaining_budget"]:
        findings.append({"id": "BUDGET-003", "severity": "block", "message": f"Projected increase ${delta:.2f} exceeds remaining team budget ${context['remaining_budget']:.2f}."})
    for change in changes:
        tags = tags_for(change)
        production = context["environment"] == "production" or tags.get("Environment", "").lower() in {"prod", "production"}
        if production and not tags.get("Owner"):
            findings.append({"id": "PROD-OWNER-001", "severity": "block", "message": f"{change['address']} is production-scoped but has no Owner tag."})
        if change["resource_type"] == "aws_db_instance" and bool(change["after"].get("publicly_accessible")):
            findings.append({"id": "PUBLIC-DB-001", "severity": "block", "message": f"{change['address']} sets publicly_accessible=true."})
        if production and change["action"] in {"delete", "replace"}:
            findings.append({"id": "PROD-DESTRUCTIVE-001", "severity": "block", "message": f"{change['action'].title()} of production resource {change['address']} requires V2 approval controls."})
    return findings


def decide(findings: list[dict[str, str]]) -> tuple[str, str]:
    decision = "BLOCK" if any(item["severity"] == "block" for item in findings) else "WARN" if findings else "ALLOW"
    if not findings: return decision, "No V1 cost or policy rule was violated by this supported Terraform change."
    return decision, f"{decision}: " + " ".join(item["message"] for item in findings)


def cost_risk(delta: float, remaining_budget: float | None) -> dict[str, Any]:
    """Score positive cost change using transparent absolute and budget-relative factors."""
    positive_delta = max(delta, 0.0)
    absolute_score = min(12, round(positive_delta / 25))
    if remaining_budget is None:
        budget_ratio, budget_score = None, 0
    elif remaining_budget <= 0:
        budget_ratio, budget_score = (float("inf") if positive_delta else 0.0), (13 if positive_delta else 0)
    else:
        budget_ratio = positive_delta / remaining_budget
        budget_score = min(13, round(budget_ratio * 13))
    return {
        "score": absolute_score + budget_score, "max": 25,
        "absolute_score": absolute_score, "budget_score": budget_score,
        "estimated_monthly_delta": round(delta, 2), "remaining_budget": remaining_budget,
        "budget_ratio": None if budget_ratio is None else ("exceeded" if budget_ratio == float("inf") else round(budget_ratio, 4)),
    }


def risk_assessment(changes: list[dict[str, Any]], delta: float, context: dict[str, Any], topology_source: TopologySource | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """CloudPilot's transparent 100-point composite heuristic, with every component exposed."""
    persisted_criticality = context.get("service_criticality", {})
    persisted_criticality = persisted_criticality if isinstance(persisted_criticality, dict) else {}
    affected_services: list[dict[str, Any]] = []
    for change in changes:
        configured = persisted_criticality.get(change["address"], [])
        affected_services.extend(item for item in configured if isinstance(configured, list) and isinstance(item, dict))
    tier1_service_ids_by_address = {
        address: [str(item["service_id"]) for item in services if item.get("criticality") == "Tier1" and item.get("service_id") is not None]
        for address, services in persisted_criticality.items()
        if isinstance(address, str) and isinstance(services, list)
    }
    radius = blast_radius(
        [change["address"] for change in changes],
        topology_source=topology_source,
        tier1_service_ids_by_address=tier1_service_ids_by_address,
    )
    cost = cost_risk(delta, context.get("remaining_budget"))
    criticality = min(25, max((int(item.get("risk_points", 0) or 0) for item in affected_services), default=0))
    dependency = min(25, radius["direct_dependencies"] * 7 + radius["indirect_dependencies"] * 3 + radius["tier1_services"] * 5)
    magnitude = min(15, sum(15 if change["action"] in {"delete", "replace"} else 8 if change["action"] == "update" else 4 for change in changes))
    production = context["environment"] == "production"
    timing = 10 if production and (criticality or dependency) and not context.get("maintenance_window_active") else 0
    score = min(100, cost["score"] + dependency + criticality + magnitude + timing)
    label = "CRITICAL" if score > 80 else "HIGH" if score > 60 else "MEDIUM" if score > 30 else "LOW"
    return {"score": score, "label": label, "components": {"cost_impact": cost, "dependency_radius": {"score": dependency, "max": 25}, "service_criticality": {"score": criticality, "max": 25}, "change_magnitude": {"score": magnitude, "max": 15}, "maintenance_timing": {"score": timing, "max": 10, "maintenance_window_active": bool(context.get("maintenance_window_active")), "window": context.get("maintenance_window")}}}, radius


def analyze(plan: dict[str, Any], context: dict[str, Any], topology_source: TopologySource | None = None) -> dict[str, Any]:
    changes = normalize_plan(plan)
    findings = evaluate_policies(changes, context, context.get("policy_catalogue") or load_policies())
    findings.extend(unsupported_resource_findings(plan))
    current, proposed = round(sum(item["current_monthly_cost"] for item in changes), 2), round(sum(item["proposed_monthly_cost"] for item in changes), 2)
    delta = round(proposed - current, 2)
    risk, radius = risk_assessment(changes, delta, context, topology_source)
    if risk["score"] > 80:
        findings.append({"id": "RISK-CRITICAL-001", "severity": "block", "message": f"V2 composite risk is {risk['score']}/100 (CRITICAL): dependency, criticality, change magnitude, or timing requires this deployment to be blocked."})
    elif risk["score"] > 60:
        findings.append({"id": "RISK-HIGH-001", "severity": "warn", "message": f"V2 composite risk is {risk['score']}/100 (HIGH): human approval is required before deployment."})
    decision, explanation = decide(findings)
    return {"decision": decision, "changes": changes, "findings": findings, "explanation": explanation,
            "current_monthly_cost": current, "proposed_monthly_cost": proposed, "monthly_cost_delta": delta,
            "risk": risk, "blast_radius": radius,
            "pricing": {"source": "repository static USD catalogue", "kind": "estimate", "supported_resources": sorted(SUPPORTED_TYPES),
                        "limitation": "Static us-east-1 estimates assume 730 hours/month and 10 GB of S3 Standard storage; excludes NAT data processing, load-balancer capacity units, S3 requests/data transfer, taxes, and discounts."}}
