"""V3 quality tests for security, topology freshness, contextual cost, and verification maths."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.config import settings
from app.cloudwatch import CostExplorerObservationError, cost_explorer_observation
from app.engines import cost_risk
from app.live_topology import DatabaseTopologySource, discover_live_topology, stored_topology
from app.models import DependencyEdge, ResourceNode
from app.redaction import REDACTED, redact_plan, redact_report
from app.verification import _dependency_scores, _health_status


def reset_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


class EmptyPaginator:
    def paginate(self, **_kwargs):
        return []


class EmptyClient:
    def get_paginator(self, _method):
        return EmptyPaginator()

    def describe_addresses(self):
        return {"Addresses": []}


class EmptyS3Client(EmptyClient):
    def list_buckets(self):
        return {"Buckets": []}


class FailingEc2Client(EmptyClient):
    def get_paginator(self, _method):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeInstances")


class EmptySession:
    def __init__(self, ec2=None):
        self.ec2 = ec2 or EmptyClient()

    def client(self, name, **_kwargs):
        if name == "sts":
            return type("Sts", (), {"get_caller_identity": lambda _self: {"Account": "123"}})()
        if name == "ec2":
            return self.ec2
        if name == "s3":
            return EmptyS3Client()
        return EmptyClient()


@pytest.mark.parametrize(("delta", "budget", "score", "ratio"), [
    (0, 100, 0, 0.0),
    (25, 10_000, 1, 0.0025),
    (100, 100, 17, 1.0),
    (101, 100, 17, 1.01),
    (100, 0, 17, "exceeded"),
    (-50, 100, 0, 0.0),
])
def test_cost_risk_is_budget_relative_and_safe(delta, budget, score, ratio):
    result = cost_risk(delta, budget)
    assert result["score"] == score
    assert result["budget_ratio"] == ratio
    assert result["max"] == 25


@pytest.mark.parametrize(("predicted", "observed", "expected"), [
    ({"a", "b"}, {"a", "b"}, (100.0, 100.0, 100.0)),
    ({"a", "b"}, {"a"}, (50.0, 100.0, 66.67)),
    ({"a"}, {"a", "b"}, (100.0, 50.0, 66.67)),
    (set(), set(), (100.0, 100.0, 100.0)),
    (set(), {"a"}, (0.0, 0.0, 0.0)),
])
def test_dependency_verification_precision_recall_and_f1(predicted, observed, expected):
    assert _dependency_scores(predicted, observed) == expected


def test_health_thresholds_are_explicit_and_include_critical():
    assert _health_status({}) == "NOT_OBSERVED"
    assert _health_status({"latency_ms_before": 100, "latency_ms_after": 110}) == "HEALTHY"
    assert _health_status({"latency_ms_before": 100, "latency_ms_after": 130}) == "DEGRADED"
    assert _health_status({"latency_ms_before": 100, "latency_ms_after": 200}) == "CRITICAL"
    assert _health_status({"error_rate_after": 0.05}) == "CRITICAL"
    assert _health_status({"availability_after": 0.98}) == "CRITICAL"


def test_sensitive_terraform_values_are_redacted_before_persistence():
    plan = {"resource_changes": [{"address": "aws_db_instance.example", "change": {
        "before": {"password": "old", "nested": {"api_key": "one"}},
        "after": {"password": "new", "nested": {"api_key": "two"}},
        "before_sensitive": {"password": True}, "after_sensitive": {"password": True},
    }}]}
    safe_plan = redact_plan(plan)
    report = {"changes": [{"address": "aws_db_instance.example", "before": plan["resource_changes"][0]["change"]["before"], "after": plan["resource_changes"][0]["change"]["after"]}]}
    safe_report = redact_report(report, plan)

    assert safe_plan["resource_changes"][0]["change"]["after"]["password"] == REDACTED
    assert safe_plan["resource_changes"][0]["change"]["after"]["nested"]["api_key"] == REDACTED
    assert safe_report["changes"][0]["before"]["password"] == REDACTED
    assert safe_report["changes"][0]["after"]["nested"]["api_key"] == REDACTED
    assert plan["resource_changes"][0]["change"]["after"]["password"] == "new"


def test_root_sensitive_terraform_objects_remain_safe_and_keep_report_contracts():
    plan = {"resource_changes": [{"address": "aws_db_instance.example", "change": {
        "before": {"instance_class": "db.t3.micro", "credential": "old"},
        "after": {"instance_class": "db.t3.small", "credential": "new"},
        "before_sensitive": True, "after_sensitive": True,
    }}]}
    report = {"changes": [{"address": "aws_db_instance.example", "before": plan["resource_changes"][0]["change"]["before"], "after": plan["resource_changes"][0]["change"]["after"]}]}

    safe_report = redact_report(report, plan)

    assert safe_report["changes"][0]["before"] == {"_sensitive": REDACTED}
    assert safe_report["changes"][0]["after"] == {"_sensitive": REDACTED}


def test_complete_sync_marks_missing_resources_inactive_and_filters_dangling_edges(monkeypatch):
    reset_database()
    monkeypatch.setattr("app.live_topology._session", lambda: EmptySession())
    db = SessionLocal()
    try:
        db.add_all([
            ResourceNode(external_id="aws_instance.gone", name="gone", resource_type="ec2", region=settings.aws_region, tags={}, metadata_json={}, is_active=True, last_seen_at=datetime.now(timezone.utc)),
            ResourceNode(external_id="aws_ebs_volume.live", name="live", resource_type="ebs", region=settings.aws_region, tags={}, metadata_json={}, is_active=True, last_seen_at=datetime.now(timezone.utc)),
            DependencyEdge(source_external_id="aws_ebs_volume.live", target_external_id="aws_instance.gone", edge_type="attached_to"),
        ])
        db.commit()
        result = discover_live_topology(db)
        stale = db.scalar(select(ResourceNode).where(ResourceNode.external_id == "aws_instance.gone"))
        assert result["stale_nodes_marked"] == 2
        assert stale is not None and stale.is_active is False
        assert DatabaseTopologySource(db).topology()["nodes"] == []
        assert stored_topology(db)["edges"] == []
    finally:
        db.close()


def test_partial_sync_does_not_deactivate_previous_topology(monkeypatch):
    reset_database()
    monkeypatch.setattr("app.live_topology._session", lambda: EmptySession(FailingEc2Client()))
    db = SessionLocal()
    try:
        db.add(ResourceNode(external_id="aws_instance.keep", name="keep", resource_type="ec2", region=settings.aws_region, tags={}, metadata_json={}, is_active=True))
        db.commit()
        result = discover_live_topology(db)
        node = db.scalar(select(ResourceNode).where(ResourceNode.external_id == "aws_instance.keep"))
        assert result["warnings"]
        assert result["stale_nodes_marked"] == 0
        assert node is not None and node.is_active is True
    finally:
        db.close()


def test_active_topology_is_limited_to_the_selected_aws_region():
    reset_database()
    db = SessionLocal()
    try:
        db.add_all([
            ResourceNode(external_id="aws_instance.current", name="current", resource_type="ec2", region=settings.aws_region, tags={}, metadata_json={}, is_active=True),
            ResourceNode(external_id="aws_instance.other", name="other", resource_type="ec2", region="other-region-1", tags={}, metadata_json={}, is_active=True),
        ])
        db.commit()
        assert [node["id"] for node in DatabaseTopologySource(db).topology()["nodes"]] == ["aws_instance.current"]
        assert [node["id"] for node in stored_topology(db)["nodes"]] == ["aws_instance.current"]
    finally:
        db.close()


class CostExplorerClient:
    def __init__(self):
        self.request = None

    def get_cost_and_usage(self, **request):
        self.request = request
        return {"ResultsByTime": [
            {"Total": {"UnblendedCost": {"Amount": "1.25"}}},
            {"Total": {"UnblendedCost": {"Amount": "2.50"}}},
        ]}


def test_cost_explorer_observation_is_scoped_and_explicit_about_attribution(monkeypatch):
    client = CostExplorerClient()

    class CostSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def client(self, name, **kwargs):
            assert name == "ce"
            assert kwargs["region_name"] == settings.cost_explorer_region
            return client

    monkeypatch.setattr("app.cloudwatch.boto3.Session", CostSession)
    observation = cost_explorer_observation(date(2026, 9, 1), date(2026, 9, 3), "Amazon Elastic Compute Cloud - Compute")

    assert observation["amount"] == 3.75
    assert observation["scope"] == "service"
    assert "not per-resource" in observation["attribution_limitation"]
    assert client.request["Filter"]["Dimensions"]["Key"] == "SERVICE"
    with pytest.raises(CostExplorerObservationError, match="end_date"):
        cost_explorer_observation(date(2026, 9, 3), date(2026, 9, 3))
