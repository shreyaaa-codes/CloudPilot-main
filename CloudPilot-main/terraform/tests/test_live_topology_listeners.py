"""Mocked ELBv2 listener discovery tests; no AWS calls are made here."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.live_topology import discover_live_topology
from app.models import DependencyEdge, ResourceNode


LB_ARN = "arn:aws:elasticloadbalancing:ap-south-1:123:loadbalancer/app/checkout/abc"
TG_ONE = "arn:aws:elasticloadbalancing:ap-south-1:123:targetgroup/checkout-one/tg1"
TG_TWO = "arn:aws:elasticloadbalancing:ap-south-1:123:targetgroup/checkout-two/tg2"


class Paginator:
    def __init__(self, pages: Callable[..., list[dict]] | list[dict]):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages(**kwargs) if callable(self.pages) else self.pages


class EmptyClient:
    def get_paginator(self, _method):
        return Paginator([])

    def describe_addresses(self):
        return {"Addresses": []}


class EmptyS3Client:
    def list_buckets(self):
        return {"Buckets": []}


class FakePagedClient(EmptyClient):
    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages

    def get_paginator(self, method):
        return Paginator(self.pages.get(method, []))

    def describe_addresses(self):
        return {"Addresses": [address for page in self.pages.get("describe_addresses", []) for address in page.get("Addresses", [])]}


class FakeS3:
    def __init__(self, buckets: list[dict], region: str, tags: dict[str, list[dict]] | None = None, list_error: Exception | None = None):
        self.buckets = buckets
        self.region = region
        self.tags = tags or {}
        self.list_error = list_error

    def list_buckets(self):
        if self.list_error:
            raise self.list_error
        return {"Buckets": self.buckets}

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": self.region}

    def get_bucket_tagging(self, Bucket):
        return {"TagSet": self.tags.get(Bucket, [])}


class FakeElbv2(EmptyClient):
    def __init__(self, target_groups: list[dict], load_balancers: list[dict], listeners_by_lb: dict[str, list[dict]] | Exception):
        self.target_groups = target_groups
        self.load_balancers = load_balancers
        self.listeners_by_lb = listeners_by_lb

    def get_paginator(self, method):
        if method == "describe_target_groups":
            return Paginator([{"TargetGroups": self.target_groups}])
        if method == "describe_load_balancers":
            return Paginator([{"LoadBalancers": self.load_balancers}])
        if method == "describe_listeners":
            def listeners(**kwargs):
                if isinstance(self.listeners_by_lb, Exception):
                    raise self.listeners_by_lb
                return [{"Listeners": self.listeners_by_lb.get(kwargs["LoadBalancerArn"], [])}]
            return Paginator(listeners)
        return super().get_paginator(method)

    def describe_target_health(self, **_kwargs):
        return {"TargetHealthDescriptions": []}


class FakeSession:
    def __init__(self, elbv2: FakeElbv2, clients: dict[str, Any] | None = None):
        self.elbv2 = elbv2
        self.clients = clients or {}

    def client(self, name, **_kwargs):
        if name == "elbv2":
            return self.elbv2
        if name == "sts":
            return type("Sts", (), {"get_caller_identity": lambda _self: {"Account": "123"}})()
        if name == "s3":
            return self.clients.get(name, EmptyS3Client())
        if name in self.clients:
            return self.clients[name]
        return EmptyClient()


def load_balancer():
    return {"LoadBalancerArn": LB_ARN, "LoadBalancerName": "checkout", "Scheme": "internet-facing", "Type": "application", "VpcId": "vpc-shared"}


def target_group(arn: str):
    return {"TargetGroupArn": arn, "TargetGroupName": arn.split("/")[-2], "VpcId": "vpc-shared"}


def listener(arn_suffix: str, actions: list[dict]):
    return {"ListenerArn": f"arn:aws:elasticloadbalancing:ap-south-1:123:listener/app/checkout/abc/{arn_suffix}", "Port": 443, "Protocol": "HTTPS", "DefaultActions": actions}


def discover(monkeypatch, groups: list[dict], listeners: dict[str, list[dict]] | Exception, clients: dict[str, Any] | None = None):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.live_topology._session", lambda: FakeSession(FakeElbv2(groups, [load_balancer()], listeners), clients))
    db = SessionLocal()
    try:
        result = discover_live_topology(db)
        edges = list(db.scalars(select(DependencyEdge).order_by(DependencyEdge.source_external_id, DependencyEdge.target_external_id)))
        nodes = list(db.scalars(select(ResourceNode)))
        return result, nodes, edges
    finally:
        db.close()


def edge_tuples(edges):
    return {(edge.edge_type, edge.inferred_from, edge.confidence) for edge in edges}


def test_alb_listener_action_links_one_target_group(monkeypatch):
    result, nodes, edges = discover(monkeypatch, [target_group(TG_ONE)], {LB_ARN: [listener("listener-1", [{"Type": "forward", "TargetGroupArn": TG_ONE}])]})

    assert result["warnings"] == []
    assert {node.resource_type for node in nodes} >= {"load_balancer", "listener", "listener_action", "target_group"}
    assert edge_tuples(edges) == {("attached_to_lb", "aws_listener", 1.0), ("attached_to_listener", "aws_listener", 1.0), ("used_by_action", "aws_listener_action", 1.0)}
    assert all(edge.edge_type != "routes_through" for edge in edges)


def test_one_alb_can_forward_one_listener_action_to_multiple_target_groups(monkeypatch):
    _result, _nodes, edges = discover(monkeypatch, [target_group(TG_ONE), target_group(TG_TWO)], {
        LB_ARN: [listener("listener-1", [{"Type": "forward", "ForwardConfig": {"TargetGroups": [{"TargetGroupArn": TG_ONE}, {"TargetGroupArn": TG_TWO}]}}])],
    })

    assert len([edge for edge in edges if edge.edge_type == "used_by_action"]) == 2


def test_listener_without_target_group_is_retained_without_forward_edge(monkeypatch):
    _result, nodes, edges = discover(monkeypatch, [], {LB_ARN: [listener("listener-1", [{"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "404"}}])]})

    assert any(node.resource_type == "listener_action" for node in nodes)
    assert not [edge for edge in edges if edge.edge_type == "used_by_action"]


def test_multiple_listeners_are_independently_linked(monkeypatch):
    _result, _nodes, edges = discover(monkeypatch, [target_group(TG_ONE), target_group(TG_TWO)], {
        LB_ARN: [
            listener("listener-1", [{"Type": "forward", "TargetGroupArn": TG_ONE}]),
            listener("listener-2", [{"Type": "forward", "TargetGroupArn": TG_TWO}]),
        ],
    })

    assert len([edge for edge in edges if edge.edge_type == "attached_to_lb"]) == 2
    assert len([edge for edge in edges if edge.edge_type == "used_by_action"]) == 2


def test_missing_target_group_does_not_create_a_dangling_relationship(monkeypatch):
    result, _nodes, edges = discover(monkeypatch, [], {LB_ARN: [listener("listener-1", [{"Type": "forward", "TargetGroupArn": TG_ONE}])]})

    assert not [edge for edge in edges if edge.edge_type == "used_by_action"]
    assert any("not returned by DescribeTargetGroups" in warning for warning in result["warnings"])


def test_listener_api_failure_is_reported_without_failing_topology_sync(monkeypatch):
    failure = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeListeners")
    result, nodes, edges = discover(monkeypatch, [target_group(TG_ONE)], failure)

    assert any("ELB listeners skipped" in warning for warning in result["warnings"])
    assert any(node.resource_type == "load_balancer" for node in nodes)
    assert not [edge for edge in edges if edge.edge_type in {"attached_to_lb", "attached_to_listener", "used_by_action"}]


def test_discovery_adds_s3_nat_eip_and_classic_elb_with_explicit_edges(monkeypatch):
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clients = {
        "ec2": FakePagedClient({
            "describe_instances": [{"Reservations": [{"Instances": [{
                "InstanceId": "i-123", "State": {"Name": "running"}, "InstanceType": "t3.micro",
            }]}]}],
            "describe_addresses": [{"Addresses": [
                {"AllocationId": "eipalloc-instance", "PublicIp": "203.0.113.10", "Domain": "vpc", "InstanceId": "i-123"},
                {"AllocationId": "eipalloc-nat", "PublicIp": "203.0.113.11", "Domain": "vpc"},
            ]}],
            "describe_nat_gateways": [{"NatGateways": [{
                "NatGatewayId": "nat-123", "State": "available", "ConnectivityType": "public",
                "VpcId": "vpc-123", "SubnetId": "subnet-123",
                "NatGatewayAddresses": [{"AllocationId": "eipalloc-nat", "PublicIp": "203.0.113.11"}],
                "Tags": [{"Key": "Name", "Value": "egress"}],
            }]}],
        }),
        "elb": FakePagedClient({"describe_load_balancers": [{"LoadBalancerDescriptions": [{
            "LoadBalancerName": "classic-web", "DNSName": "classic.example.test",
            "Scheme": "internet-facing", "VPCId": "vpc-123", "Subnets": ["subnet-123"],
            "AvailabilityZones": ["ap-south-1a"], "SecurityGroups": ["sg-123"],
            "Instances": [{"InstanceId": "i-123"}],
        }]}]}),
        "s3": FakeS3(
            [{"Name": "logs-bucket", "CreationDate": created_at}], settings.aws_region,
            {"logs-bucket": [{"Key": "Environment", "Value": "production"}]},
        ),
    }

    result, nodes, edges = discover(monkeypatch, [], {}, clients)

    nodes_by_id = {node.external_id: node for node in nodes}
    assert result["warnings"] == []
    assert {
        "aws_eip.eipalloc-instance", "aws_eip.eipalloc-nat", "aws_nat_gateway.nat-123",
        "aws_elb.classic-web", "aws_lb.abc", "aws_s3_bucket.logs-bucket",
    } <= nodes_by_id.keys()
    assert nodes_by_id["aws_s3_bucket.logs-bucket"].tags == {"Environment": "production"}
    assert nodes_by_id["aws_s3_bucket.logs-bucket"].metadata_json == {
        "region": settings.aws_region, "creation_date": created_at.isoformat(),
    }
    assert nodes_by_id["aws_eip.eipalloc-instance"].metadata_json["public_ip"] == "203.0.113.10"
    assert nodes_by_id["aws_nat_gateway.nat-123"].metadata_json["subnet_id"] == "subnet-123"
    assert nodes_by_id["aws_elb.classic-web"].metadata_json["dns_name"] == "classic.example.test"
    assert nodes_by_id["aws_lb.abc"].metadata_json["type"] == "application"
    edge_ids = {(edge.source_external_id, edge.target_external_id, edge.edge_type) for edge in edges}
    assert ("aws_eip.eipalloc-instance", "aws_instance.i-123", "associated_with") in edge_ids
    assert ("aws_nat_gateway.nat-123", "aws_eip.eipalloc-nat", "uses_address") in edge_ids
    assert ("aws_instance.i-123", "aws_elb.classic-web", "registered_with") in edge_ids


def test_s3_access_failure_is_reported_without_failing_topology_sync(monkeypatch):
    failure = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListBuckets")

    result, _nodes, _edges = discover(monkeypatch, [], {}, {"s3": FakeS3([], settings.aws_region, list_error=failure)})

    assert any("S3 skipped" in warning for warning in result["warnings"])
    assert result["stale_nodes_marked"] == 0
