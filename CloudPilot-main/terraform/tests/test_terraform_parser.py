"""Focused audit tests for Terraform's resource_changes normalisation contract."""
import copy

import pytest

from app.engines import action_name, analyze, changed_attributes, normalize_plan, resource_monthly_cost
from app.graph import FixtureTopologySource, blast_radius


@pytest.mark.parametrize(("actions", "expected"), [
    (["create"], "create"),
    (["update"], "update"),
    (["delete"], "delete"),
    (["create", "delete"], "replace"),
    (["delete", "create"], "replace"),
    (["no-op"], "unknown"),
    (["read"], "unknown"),
    (["create", "delete", "create"], "unknown"),
    ("create", "unknown"),
    (None, "unknown"),
])
def test_action_name_handles_terraform_actions_and_rejects_invalid_shapes(actions, expected):
    assert action_name(actions) == expected


def test_changed_attributes_is_sorted_and_covers_added_removed_and_nested_keys():
    before = {"instance_type": "t3.small", "obsolete": True, "tags": {"Owner": "A", "Environment": "prod"}, "subnets": ["subnet-a"]}
    after = {"instance_type": "m6i.large", "new_setting": 1, "tags": {"Owner": "B", "Tier": "1"}, "subnets": ["subnet-a", "subnet-b"]}
    assert changed_attributes(before, after) == ["instance_type", "new_setting", "obsolete", "subnets[1]", "tags.Environment", "tags.Owner", "tags.Tier"]
    assert changed_attributes(None, ["not", "an", "object"]) == []


def test_monthly_cost_supports_catalogue_resources_and_safe_invalid_ebs_size():
    assert resource_monthly_cost("aws_instance", {"instance_type": "t3.micro"}) == 7.59
    assert resource_monthly_cost("aws_db_instance", {"instance_class": "db.r6g.large"}) == 138.70
    assert resource_monthly_cost("aws_ebs_volume", {"size": "20", "type": "gp3"}) == 1.6
    assert resource_monthly_cost("aws_ebs_volume", {"size": "unknown", "type": "gp3"}) == 0
    assert resource_monthly_cost("aws_instance", None) == 0


@pytest.mark.parametrize(("resource_type", "values", "expected"), [
    ("aws_nat_gateway", {}, 32.85),
    ("aws_eip", {}, 3.65),
    ("aws_lb", {"load_balancer_type": "application"}, 16.425),
    ("aws_lb", {"load_balancer_type": "network"}, 16.425),
    ("aws_lb", {"load_balancer_type": "gateway"}, 9.125),
    ("aws_s3_bucket", {}, 0.23),
])
def test_monthly_cost_estimates_new_aws_resources(resource_type, values, expected):
    assert resource_monthly_cost(resource_type, values) == pytest.approx(expected)


def test_normalize_plan_includes_newly_supported_aws_resources():
    plan = {"resource_changes": [
        {"address": "aws_nat_gateway.main", "type": "aws_nat_gateway", "change": {"actions": ["create"], "before": None, "after": {}}},
        {"address": "aws_eip.public", "type": "aws_eip", "change": {"actions": ["create"], "before": None, "after": {}}},
        {"address": "aws_lb.app", "type": "aws_lb", "change": {"actions": ["create"], "before": None, "after": {"load_balancer_type": "application"}}},
        {"address": "aws_s3_bucket.logs", "type": "aws_s3_bucket", "change": {"actions": ["create"], "before": None, "after": {}}},
    ]}

    changes = normalize_plan(plan)

    assert [change["resource_type"] for change in changes] == ["aws_nat_gateway", "aws_eip", "aws_lb", "aws_s3_bucket"]
    assert [change["proposed_monthly_cost"] for change in changes] == [32.85, 3.65, 16.43, 0.23]


def test_normalize_plan_calculates_create_delete_update_and_replace_without_mutating_input():
    plan = {"resource_changes": [
        {"address": "aws_instance.web", "type": "aws_instance", "change": {"actions": ["create"], "before": None, "after": {"instance_type": "t3.small"}}},
        {"address": "aws_db_instance.old", "type": "aws_db_instance", "change": {"actions": ["delete"], "before": {"instance_class": "db.t3.medium"}, "after": None}},
        {"address": "aws_ebs_volume.data", "type": "aws_ebs_volume", "change": {"actions": ["update"], "before": {"size": 10, "type": "gp2"}, "after": {"size": 20, "type": "io1"}}},
        {"address": "aws_instance.worker", "type": "aws_instance", "change": {"actions": ["delete", "create"], "before": {"instance_type": "t3.micro"}, "after": {"instance_type": "m6i.large"}}},
    ]}
    original = copy.deepcopy(plan)

    changes = normalize_plan(plan)

    assert plan == original
    assert [(item["address"], item["action"], item["current_monthly_cost"], item["proposed_monthly_cost"], item["monthly_delta"]) for item in changes] == [
        ("aws_instance.web", "create", 0.0, 15.18, 15.18),
        ("aws_db_instance.old", "delete", 61.32, 0.0, -61.32),
        ("aws_ebs_volume.data", "update", 1.0, 2.5, 1.5),
        ("aws_instance.worker", "replace", 7.59, 70.08, 62.49),
    ]
    assert changes[2]["changed_attributes"] == ["size", "type"]


def test_normalize_plan_skips_noops_reads_unsupported_and_malformed_entries():
    plan = {"resource_changes": [
        {"address": "aws_instance.noop", "type": "aws_instance", "change": {"actions": ["no-op"], "before": {}, "after": {}}},
        {"address": "aws_instance.read", "type": "aws_instance", "change": {"actions": ["read"], "before": {}, "after": {}}},
        {"address": "data.aws_instance.lookup", "mode": "data", "type": "aws_instance", "change": {"actions": ["create"], "before": {}, "after": {"instance_type": "t3.micro"}}},
        {"address": "aws_dynamodb_table.logs", "type": "aws_dynamodb_table", "change": {"actions": ["create"], "before": {}, "after": {}}},
        {"address": "aws_instance.bad", "type": "aws_instance", "change": []},
        "not-an-object",
        {"type": "aws_ebs_volume", "change": {"actions": ["create"], "before": [], "after": {"size": 5, "type": "gp3"}}},
    ]}

    changes = normalize_plan(plan)

    assert len(changes) == 1
    assert changes[0]["address"] == "aws_ebs_volume"
    assert changes[0]["monthly_delta"] == 0.4
    assert normalize_plan({}) == []
    assert normalize_plan({"resource_changes": {}}) == []
    assert normalize_plan([]) == []


def test_analyze_warns_for_unsupported_resources_without_assigning_them_cost():
    plan = {"resource_changes": [
        {"address": "aws_dynamodb_table.logs", "type": "aws_dynamodb_table", "change": {"actions": ["create"], "before": None, "after": {}}},
    ]}

    topology_source = FixtureTopologySource({"nodes": [], "edges": []})
    report = analyze(plan, {"environment": "development"}, topology_source=topology_source)

    assert report["decision"] == "WARN"
    assert report["changes"] == []
    assert report["monthly_cost_delta"] == 0.0
    finding = next(item for item in report["findings"] if item.get("code") == "UNSUPPORTED_RESOURCE")
    assert finding["severity"] == "warn"
    assert finding["address"] == "aws_dynamodb_table.logs"
    assert finding["resource_type"] == "aws_dynamodb_table"
    assert finding["action"] == "create"
    assert "cost estimation is not supported" in finding["message"].lower()


def test_unsupported_resource_warning_preserves_independent_policy_block():
    plan = {"resource_changes": [
        {"address": "aws_dynamodb_table.logs", "type": "aws_dynamodb_table", "change": {"actions": ["create"], "before": None, "after": {}}},
        {"address": "aws_instance.app", "type": "aws_instance", "change": {"actions": ["create"], "before": None, "after": {"instance_type": "m6i.large"}}},
    ]}

    topology_source = FixtureTopologySource({"nodes": [], "edges": []})
    report = analyze(plan, {"environment": "development", "remaining_budget": 10}, topology_source=topology_source)

    assert report["decision"] == "BLOCK"
    assert any(item.get("code") == "UNSUPPORTED_RESOURCE" and item["severity"] == "warn" for item in report["findings"])
    assert any(item["id"] == "BUDGET-003" and item["severity"] == "block" for item in report["findings"])


def test_normalize_plan_accepts_missing_mode_as_managed_and_filters_explicit_non_managed_modes():
    plan = {"resource_changes": [
        {"address": "aws_instance.default_mode", "type": "aws_instance", "change": {"actions": ["create"], "before": {}, "after": {"instance_type": "t3.micro"}}},
        {"address": "data.aws_instance.lookup", "mode": "data", "type": "aws_instance", "change": {"actions": ["create"], "before": {}, "after": {"instance_type": "t3.micro"}}},
        {"address": "aws_instance.invalid_mode", "mode": None, "type": "aws_instance", "change": {"actions": ["create"], "before": {}, "after": {"instance_type": "t3.micro"}}},
    ]}

    assert [change["address"] for change in normalize_plan(plan)] == ["aws_instance.default_mode"]


def test_fixture_topology_source_is_explicit_and_deterministic_for_unit_tests():
    fixture = {"nodes": [{"id": "aws_instance.test"}, {"id": "service.test", "criticality": "Tier1"}],
               "edges": [{"source": "aws_instance.test", "target": "service.test", "type": "depends_on"}]}

    radius = blast_radius(["aws_instance.test"], topology_source=FixtureTopologySource(fixture))

    assert radius["topology_source"] == "fixture"
    assert radius["changed_nodes"] == ["aws_instance.test"]
    assert radius["direct_dependencies"] == 1
    assert radius["paths"] == [["aws_instance.test", "service.test"]]
    assert radius["tier1_services"] == 0


def test_blast_radius_calculates_distinct_tier1_services_from_explicit_business_context():
    fixture = {"nodes": [{"id": "aws_instance.root"}, {"id": "service.a"}, {"id": "service.b"}],
               "edges": [{"source": "aws_instance.root", "target": "service.a", "type": "depends_on"},
                         {"source": "service.a", "target": "service.b", "type": "depends_on"}]}

    radius = blast_radius(
        ["aws_instance.root"],
        topology_source=FixtureTopologySource(fixture),
        tier1_service_ids_by_address={
            "aws_instance.root": ["checkout"],
            "service.a": ["payments"],
            "service.b": ["checkout"],
        },
    )

    assert radius["tier1_services"] == 2
