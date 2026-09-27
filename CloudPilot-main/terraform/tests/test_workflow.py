import os
os.environ["DATABASE_URL"] = "sqlite:///./test_cloudpilot.db"
import json
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.database import Base, SessionLocal, engine
from app.config import settings
from app.main import app
from app.models import ChangeAnalysis, DependencyEdge, ResourceNode, Service, ServiceCriticality, ServiceResource

client = TestClient(app)
PLAN = json.loads((Path(__file__).parent.parent / "test-plans" / "rds-scale-up.json").read_text())

def reset_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed_live_checkout_topology():
    """Persist a graph deliberately unlike the JSON fixture for analysis tests."""
    db = SessionLocal()
    try:
        db.add_all([
            ResourceNode(external_id="aws_db_instance.checkout", name="checkout", resource_type="rds", region=settings.aws_region, tags={"Environment": "production", "Criticality": "Tier1"}, metadata_json={}),
            ResourceNode(external_id="service.live-checkout", name="live-checkout", resource_type="service", region=settings.aws_region, tags={"Environment": "production", "Criticality": "Tier1"}, metadata_json={}),
            ResourceNode(external_id="service.live-payments", name="live-payments", resource_type="service", region=settings.aws_region, tags={"Environment": "production", "Criticality": "Tier1"}, metadata_json={}),
            ResourceNode(external_id="audience.live-users", name="live-users", resource_type="audience", region=settings.aws_region, tags={"Environment": "production", "Criticality": "Tier1"}, metadata_json={}),
            DependencyEdge(source_external_id="aws_db_instance.checkout", target_external_id="service.live-checkout", edge_type="depends_on"),
            DependencyEdge(source_external_id="aws_db_instance.checkout", target_external_id="service.live-payments", edge_type="depends_on"),
            DependencyEdge(source_external_id="service.live-checkout", target_external_id="audience.live-users", edge_type="serves"),
        ])
        db.commit()
    finally:
        db.close()

def test_production_plan_is_blocked_with_cost_and_budget_explanation():
    reset_database()
    seed_live_checkout_topology()
    response = client.post("/api/analyses", json={"plan": PLAN, "context": {"project": "payments", "pull_request": "142", "environment": "production", "team": "Payments", "remaining_budget": 100, "actor": "tester"}})
    assert response.status_code == 201
    analysis = response.json()
    assert analysis["decision"] == "BLOCK"
    assert analysis["monthly_cost_delta"] == 493.48
    assert {item["id"] for item in analysis["findings"]} >= {"BUDGET-002", "BUDGET-003"}
    assert analysis["risk"]["label"] == "CRITICAL"
    assert analysis["blast_radius"]["direct_dependencies"] == 2
    assert analysis["blast_radius"]["tier1_services"] >= 1
    assert analysis["blast_radius"]["topology_source"] == "live_database"
    comment = client.get(f"/api/analyses/{analysis['id']}/github-comment")
    assert comment.status_code == 200
    assert "CloudPilot Change Analysis" in comment.text
    assert "BLOCK" in comment.text

def test_small_development_ebs_change_is_allowed():
    reset_database()
    plan = {"resource_changes": [{"address": "aws_ebs_volume.cache", "type": "aws_ebs_volume", "change": {"actions": ["update"], "before": {"size": 10, "type": "gp3", "tags": {}}, "after": {"size": 20, "type": "gp3", "tags": {}}}}]}
    response = client.post("/api/analyses", json={"plan": plan, "context": {"environment": "development"}})
    assert response.status_code == 201
    assert response.json()["decision"] == "ALLOW"
    assert response.json()["monthly_cost_delta"] == 0.8


def test_checkout_risk_changes_when_its_persisted_criticality_changes():
    reset_database()
    context = {"environment": "production", "remaining_budget": 1000}
    tier1 = client.post("/api/analyses", json={"plan": PLAN, "context": context}).json()

    db = SessionLocal()
    try:
        checkout = db.scalar(select(Service).where(Service.slug == "checkout-api"))
        tier3 = db.scalar(select(ServiceCriticality).where(ServiceCriticality.name == "Tier3"))
        assert checkout is not None and tier3 is not None
        checkout.criticality_id = tier3.id
        db.commit()
    finally:
        db.close()

    tier3_result = client.post("/api/analyses", json={"plan": PLAN, "context": context}).json()
    assert tier1["risk"]["components"]["service_criticality"]["score"] == 25
    assert tier3_result["risk"]["components"]["service_criticality"]["score"] == 0
    assert tier3_result["risk"]["score"] < tier1["risk"]["score"]


def test_another_service_can_be_independently_configured_as_tier1():
    reset_database()
    db = SessionLocal()
    try:
        # Initial analysis creates the standard business-context seed records.
        client.post("/api/analyses", json={"plan": {"resource_changes": []}, "context": {"environment": "development"}})
        checkout = db.scalar(select(Service).where(Service.slug == "checkout-api"))
        tier1 = db.scalar(select(ServiceCriticality).where(ServiceCriticality.name == "Tier1"))
        assert checkout is not None and tier1 is not None
        db.add(Service(slug="reporting-api", name="Reporting API", environment="production", team_id=checkout.team_id, criticality_id=tier1.id))
        db.flush()
        reporting = db.scalar(select(Service).where(Service.slug == "reporting-api"))
        assert reporting is not None
        db.add(ServiceResource(service_id=reporting.id, resource_external_id="aws_instance.reporting"))
        db.commit()
    finally:
        db.close()

    plan = {"resource_changes": [{"address": "aws_instance.reporting", "type": "aws_instance", "change": {
        "actions": ["create"], "before": {}, "after": {"instance_type": "t3.micro", "tags": {"Owner": "Platform"}},
    }}]}
    result = client.post("/api/analyses", json={"plan": plan, "context": {"environment": "production", "remaining_budget": 1000}})
    assert result.status_code == 201
    assert result.json()["risk"]["components"]["service_criticality"]["score"] == 25


def test_analysis_uses_persisted_topology_and_never_falls_back_to_fixture():
    reset_database()
    # This topology deliberately uses names absent from topology/default.json.
    seed_live_checkout_topology()
    response = client.post("/api/analyses", json={"plan": PLAN, "context": {"environment": "production"}})
    assert response.status_code == 201
    radius = response.json()["blast_radius"]
    assert radius["topology_source"] == "live_database"
    assert radius["changed_nodes"] == ["aws_db_instance.checkout"]
    assert [node["id"] for node in radius["impacted_nodes"]] == ["service.live-checkout", "service.live-payments", "audience.live-users"]

    reset_database()
    empty_graph_response = client.post("/api/analyses", json={"plan": PLAN, "context": {"environment": "production"}})
    assert empty_graph_response.status_code == 201
    assert empty_graph_response.json()["blast_radius"]["topology_source"] == "live_database"
    assert empty_graph_response.json()["blast_radius"]["impacted_nodes"] == []


def test_stored_live_topology_is_available_from_the_api():
    reset_database()
    db = SessionLocal()
    try:
        db.add_all([
            ResourceNode(external_id="aws_instance.i-123", name="i-123", resource_type="ec2", region=settings.aws_region, tags={}, metadata_json={}),
            ResourceNode(external_id="aws_ebs_volume.vol-123", name="vol-123", resource_type="ebs", region=settings.aws_region, tags={}, metadata_json={}),
            DependencyEdge(source_external_id="aws_ebs_volume.vol-123", target_external_id="aws_instance.i-123", edge_type="attached_to"),
        ])
        db.commit()
    finally:
        db.close()

    response = client.get("/api/topology")
    assert response.status_code == 200
    assert response.json()["nodes"][0]["id"] == "aws_ebs_volume.vol-123"
    assert response.json()["edges"] == [{"source": "aws_ebs_volume.vol-123", "target": "aws_instance.i-123", "type": "attached_to", "confidence": 0.8}]


def test_v3_compares_prediction_to_post_deployment_outcome_and_updates_confidence():
    reset_database()
    created = client.post("/api/analyses", json={"plan": PLAN, "context": {"project": "payments", "environment": "production", "remaining_budget": 100}})
    analysis = created.json()
    predicted_nodes = [node["id"] for node in analysis["blast_radius"]["impacted_nodes"]]
    verified = client.post(f"/api/analyses/{analysis['id']}/verify", json={
        "actual_monthly_cost_delta": 520,
        "observed_affected_resources": predicted_nodes,
        "telemetry": {"latency_ms_before": 100, "latency_ms_after": 130, "error_rate_before": 0.001, "error_rate_after": 0.003},
    })
    assert verified.status_code == 201
    result = verified.json()
    assert result["cost_error_amount"] == 26.52
    assert result["dependency_f1"] == 100
    assert result["health_status"] == "DEGRADED"
    assert result["risk_assessment"] == "RISK_CONFIRMED"
    assert result["confidence_after"] < 100
    assert client.get("/api/verifications").json()[0]["analysis_id"] == analysis["id"]
    assert client.post(f"/api/analyses/{analysis['id']}/verify", json={"actual_monthly_cost_delta": 520}).status_code == 409


def test_v3_rejects_a_deployment_identifier_already_used_by_another_analysis():
    reset_database()
    first = client.post("/api/analyses", json={"plan": PLAN, "context": {"environment": "production"}}).json()
    second = client.post("/api/analyses", json={"plan": PLAN, "context": {"environment": "production"}}).json()
    assert client.post(f"/api/analyses/{first['id']}/verify", json={"deployment_identifier": "release-42", "actual_monthly_cost_delta": 500}).status_code == 201
    duplicate = client.post(f"/api/analyses/{second['id']}/verify", json={"deployment_identifier": "release-42", "actual_monthly_cost_delta": 500})
    assert duplicate.status_code == 409
    assert "deployment identifier" in duplicate.json()["detail"].lower()


def test_v3_persists_and_returns_analysis_deployment_association():
    reset_database()
    analysis = client.post("/api/analyses", json={
        "plan": {"resource_changes": []}, "context": {"environment": "development"},
    }).json()

    verified = client.post(f"/api/analyses/{analysis['id']}/verify", json={
        "deployment_identifier": "deployment-123", "actual_monthly_cost_delta": 0,
    })

    assert verified.status_code == 201
    assert client.get(f"/api/analyses/{analysis['id']}").json()["deployment_id"] == "deployment-123"


def test_analysis_response_and_persisted_plan_redact_sensitive_terraform_values():
    reset_database()
    plan = {"resource_changes": [{"address": "aws_db_instance.private", "type": "aws_db_instance", "change": {
        "actions": ["update"], "before": {"instance_class": "db.t3.micro", "password": "old"},
        "after": {"instance_class": "db.t3.small", "password": "new"}, "before_sensitive": {"password": True},
        "after_sensitive": {"password": True},
    }}]}
    response = client.post("/api/analyses", json={"plan": plan, "context": {"environment": "development"}})
    assert response.status_code == 201
    analysis = response.json()
    assert analysis["changes"][0]["after"]["password"] == "[REDACTED]"
    db = SessionLocal()
    try:
        stored = db.get(ChangeAnalysis, analysis["id"])
        assert stored is not None and stored.plan["resource_changes"][0]["change"]["after"]["password"] == "[REDACTED]"
    finally:
        db.close()


def test_v3_can_return_an_explicit_read_only_cloudwatch_observation(monkeypatch):
    monkeypatch.setattr("app.main.ec2_cpu_observation", lambda instance_id, window_minutes: {
        "source": "AWS/EC2 CPUUtilization", "instance_id": instance_id, "region": "eu-north-1",
        "window_minutes": window_minutes, "datapoints": 3, "average_percent": 12.5,
        "minimum_percent": 8.0, "maximum_percent": 18.0, "observed_at": "2026-09-01T00:00:00+00:00",
    })
    response = client.post("/api/telemetry/ec2-cpu", json={"instance_id": "i-0123456789abcdef0"})
    assert response.status_code == 200
    assert response.json()["average_percent"] == 12.5


def test_v3_can_return_a_cost_explorer_signal_without_claiming_resource_attribution(monkeypatch):
    monkeypatch.setattr("app.main.cost_explorer_observation", lambda start, end, service: {
        "source": "AWS Cost Explorer / UnblendedCost", "scope": "account", "service": service,
        "start_date": start.isoformat(), "end_date_exclusive": end.isoformat(), "currency": "USD",
        "amount": 12.34, "periods": 2, "observed_at": "2026-09-04T00:00:00+00:00",
        "attribution_limitation": "This is account cost, not per-resource attribution.",
    })
    response = client.post("/api/telemetry/cost-explorer", json={"start_date": "2026-09-01", "end_date": "2026-09-03"})
    assert response.status_code == 200
    assert response.json()["amount"] == 12.34
    assert "not per-resource" in response.json()["attribution_limitation"]
