"""Read-only discovery of a bounded, defensible AWS dependency graph."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import DependencyEdge, ResourceNode, TopologySync


class TopologyDiscoveryError(RuntimeError):
    pass


class DatabaseTopologySource:
    """Live AWS topology persisted by discovery, queried at analysis time."""

    name = "live_database"

    def __init__(self, db: Session):
        self.db = db

    def topology(self) -> dict[str, list[dict[str, Any]]]:
        nodes = []
        node_id_map = {}
        for node in self.db.scalars(select(ResourceNode).where(
            ResourceNode.is_active.is_(True),
            ResourceNode.region == settings.aws_region,
        ).order_by(ResourceNode.external_id)):
            metadata = node.metadata_json or {}
            tags = node.tags or {}
            node_id = node.terraform_address or node.external_id
            node_id_map[node.external_id] = node_id
            nodes.append({
                "id": node_id, "name": node.name, "kind": node.resource_type,
                "tags": tags, "metadata": metadata,
                "environment": metadata.get("environment") or tags.get("Environment", "").lower(),
                "criticality": metadata.get("criticality") or tags.get("Criticality"),
                "owner": metadata.get("owner") or tags.get("Owner"),
            })
        return {"nodes": nodes,
                "edges": [{"source": node_id_map[edge.source_external_id], "target": node_id_map[edge.target_external_id], "type": edge.edge_type}
                          for edge in self.db.scalars(select(DependencyEdge).order_by(DependencyEdge.source_external_id, DependencyEdge.target_external_id))
                          if edge.source_external_id in node_id_map and edge.target_external_id in node_id_map]}


def _tags(items: list[dict] | None) -> dict[str, str]:
    return {item["Key"]: item["Value"] for item in items or [] if "Key" in item and "Value" in item}


def _session() -> boto3.Session:
    return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)


def _terraform_address_from_tags(resource_type: str, tags: dict) -> str | None:
    name = tags.get("Name", "")
    if not name:
        return None
    slug = name.lower().replace("-", "_").replace(" ", "_")
    return f"{resource_type}.{slug}"


def _upsert_node(
    db: Session, external_id: str, name: str, resource_type: str, tags: dict, metadata: dict,
    *, terraform_address: str | None = None, seen_ids: set[str] | None = None, seen_at: datetime | None = None,
) -> None:
    if seen_ids is not None:
        seen_ids.add(external_id)
    observed_at = seen_at or datetime.now(timezone.utc)
    node = db.scalar(select(ResourceNode).where(ResourceNode.external_id == external_id))
    if node is None:
        db.add(ResourceNode(
            external_id=external_id, name=name, resource_type=resource_type, region=settings.aws_region,
            tags=tags, metadata_json=metadata, is_active=True, last_seen_at=observed_at,
            terraform_address=terraform_address,
        ))
    else:
        node.name, node.resource_type, node.region, node.tags, node.metadata_json = name, resource_type, settings.aws_region, tags, metadata
        node.is_active, node.last_seen_at, node.discovered_at = True, observed_at, observed_at
        node.terraform_address = terraform_address


def _upsert_edge(
    db: Session, source: str, target: str, edge_type: str, *,
    inferred_from: str = "aws_metadata", confidence: float = 0.8,
) -> None:
    edge = db.scalar(select(DependencyEdge).where(DependencyEdge.source_external_id == source, DependencyEdge.target_external_id == target, DependencyEdge.edge_type == edge_type))
    if edge is None:
        db.add(DependencyEdge(
            source_external_id=source, target_external_id=target, edge_type=edge_type,
            inferred_from=inferred_from, confidence=confidence,
        ))
    else:
        edge.inferred_from, edge.confidence = inferred_from, confidence
        edge.discovered_at = datetime.now(timezone.utc)


def _page(client: Any, method: str, key: str, warnings: list[str], service: str, **kwargs):
    try:
        for page in client.get_paginator(method).paginate(**kwargs):
            yield from page.get(key, [])
    except (BotoCoreError, ClientError) as exc:
        warnings.append(f"{service} skipped: {exc}")


def _target_group_arns(action: dict[str, Any]) -> list[str]:
    """Return every target group explicitly selected by one listener action."""
    arns: list[str] = []
    direct = action.get("TargetGroupArn")
    if isinstance(direct, str):
        arns.append(direct)
    forward_config = action.get("ForwardConfig")
    if isinstance(forward_config, dict):
        for target in forward_config.get("TargetGroups", []):
            arn = target.get("TargetGroupArn") if isinstance(target, dict) else None
            if isinstance(arn, str):
                arns.append(arn)
    return list(dict.fromkeys(arns))


def discover_live_topology(db: Session) -> dict:
    """Discover a bounded relationship set; never make a modifying AWS call."""
    warnings: list[str] = []
    sync_started_at = datetime.now(timezone.utc)
    seen_node_ids: set[str] = set()
    # Older releases wrote this edge from a shared VPC alone. It is not an AWS
    # dependency and must not survive a corrected discovery run.
    db.execute(delete(DependencyEdge).where(
        DependencyEdge.edge_type == "routes_through",
        DependencyEdge.inferred_from == "aws_metadata",
    ))
    try:
        session = _session()
    except BotoCoreError as exc:
        raise TopologyDiscoveryError(f"AWS session could not be created: {exc}") from exc
    ec2 = session.client("ec2", region_name=settings.aws_region)
    rds = session.client("rds", region_name=settings.aws_region)
    autoscaling = session.client("autoscaling", region_name=settings.aws_region)
    elb = session.client("elb", region_name=settings.aws_region)
    elbv2 = session.client("elbv2", region_name=settings.aws_region)
    lambdas = session.client("lambda", region_name=settings.aws_region)
    s3 = session.client("s3", region_name=settings.aws_region)
    account = "unknown"
    try:
        account = session.client("sts", region_name=settings.aws_region).get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as exc:
        warnings.append(f"Identity check skipped: {exc}")

    instances: set[str] = set()
    for reservation in _page(ec2, "describe_instances", "Reservations", warnings, "EC2"):
        for item in reservation.get("Instances", []):
            identifier = f"aws_instance.{item['InstanceId']}"
            instances.add(item["InstanceId"])
            tags = _tags(item.get("Tags"))
            tf_address = _terraform_address_from_tags("aws_instance", tags)
            _upsert_node(db, identifier, item["InstanceId"], "ec2", tags, {"state": item["State"]["Name"], "instance_type": item["InstanceType"]}, terraform_address=tf_address, seen_ids=seen_node_ids, seen_at=sync_started_at)

    eip_ids: dict[str, str] = {}
    try:
        addresses = ec2.describe_addresses().get("Addresses", [])
    except (BotoCoreError, ClientError) as exc:
        warnings.append(f"Elastic IP skipped: {exc}")
        addresses = []
    for address in addresses:
        allocation_id = address.get("AllocationId")
        public_ip = address.get("PublicIp")
        suffix = allocation_id or public_ip
        if not isinstance(suffix, str):
            continue
        identifier = f"aws_eip.{suffix}"
        tags = _tags(address.get("Tags"))
        tf_address = _terraform_address_from_tags("aws_eip", tags)
        _upsert_node(db, identifier, suffix, "elastic_ip", tags, {
            "allocation_id": allocation_id, "public_ip": public_ip, "domain": address.get("Domain"),
            "association_id": address.get("AssociationId"),
            "network_interface_id": address.get("NetworkInterfaceId"),
            "private_ip_address": address.get("PrivateIpAddress"),
        }, terraform_address=tf_address, seen_ids=seen_node_ids, seen_at=sync_started_at)
        if isinstance(allocation_id, str):
            eip_ids[allocation_id] = identifier
        instance_id = address.get("InstanceId")
        if instance_id in instances:
            _upsert_edge(db, identifier, f"aws_instance.{instance_id}", "associated_with", inferred_from="eip_association", confidence=1.0)

    for gateway in _page(ec2, "describe_nat_gateways", "NatGateways", warnings, "NAT Gateway"):
        gateway_id = gateway.get("NatGatewayId")
        if not isinstance(gateway_id, str):
            continue
        identifier = f"aws_nat_gateway.{gateway_id}"
        tags = _tags(gateway.get("Tags"))
        tf_address = _terraform_address_from_tags("aws_nat_gateway", tags)
        gateway_addresses = gateway.get("NatGatewayAddresses", [])
        _upsert_node(db, identifier, gateway_id, "nat_gateway", tags, {
            "state": gateway.get("State"), "connectivity_type": gateway.get("ConnectivityType"),
            "vpc_id": gateway.get("VpcId"), "subnet_id": gateway.get("SubnetId"),
            "public_ips": [item.get("PublicIp") for item in gateway_addresses if isinstance(item, dict) and item.get("PublicIp")],
        }, terraform_address=tf_address, seen_ids=seen_node_ids, seen_at=sync_started_at)
        for address in gateway_addresses:
            allocation_id = address.get("AllocationId") if isinstance(address, dict) else None
            if not isinstance(allocation_id, str):
                continue
            eip_id = eip_ids.get(allocation_id)
            if eip_id is None:
                warnings.append(f"NAT Gateway {gateway_id} references EIP allocation not returned by DescribeAddresses: {allocation_id}")
                continue
            _upsert_edge(db, identifier, eip_id, "uses_address", inferred_from="nat_gateway_address", confidence=1.0)

    for volume in _page(ec2, "describe_volumes", "Volumes", warnings, "EBS"):
        volume_id = f"aws_ebs_volume.{volume['VolumeId']}"
        tags = _tags(volume.get("Tags"))
        tf_address = _terraform_address_from_tags("aws_ebs_volume", tags)
        _upsert_node(db, volume_id, volume["VolumeId"], "ebs", tags, {"size_gib": volume["Size"], "state": volume["State"]}, terraform_address=tf_address, seen_ids=seen_node_ids, seen_at=sync_started_at)
        for attachment in volume.get("Attachments", []):
            instance_id = attachment.get("InstanceId")
            if instance_id in instances:
                _upsert_edge(db, volume_id, f"aws_instance.{instance_id}", "attached_to")

    rds_ids: dict[str, str] = {}
    for item in _page(rds, "describe_db_instances", "DBInstances", warnings, "RDS"):
        identifier = f"aws_db_instance.{item['DBInstanceIdentifier']}"
        rds_ids[item["DBInstanceIdentifier"]] = identifier
        tf_address = f"aws_db_instance.{item['DBInstanceIdentifier']}"
        _upsert_node(db, identifier, item["DBInstanceIdentifier"], "rds", {}, {"engine": item["Engine"], "instance_class": item["DBInstanceClass"], "status": item["DBInstanceStatus"]}, terraform_address=tf_address, seen_ids=seen_node_ids, seen_at=sync_started_at)

    for group in _page(autoscaling, "describe_auto_scaling_groups", "AutoScalingGroups", warnings, "Auto Scaling"):
        group_id = f"aws_autoscaling_group.{group['AutoScalingGroupName']}"
        _upsert_node(db, group_id, group["AutoScalingGroupName"], "autoscaling", {tag["Key"]: tag["Value"] for tag in group.get("Tags", [])}, {"desired_capacity": group["DesiredCapacity"]}, seen_ids=seen_node_ids, seen_at=sync_started_at)
        for item in group.get("Instances", []):
            if item["InstanceId"] in instances:
                _upsert_edge(db, group_id, f"aws_instance.{item['InstanceId']}", "manages")

    for balancer in _page(elb, "describe_load_balancers", "LoadBalancerDescriptions", warnings, "Classic ELB"):
        name = balancer.get("LoadBalancerName")
        if not isinstance(name, str):
            continue
        identifier = f"aws_elb.{name}"
        _upsert_node(db, identifier, name, "load_balancer", {}, {
            "dns_name": balancer.get("DNSName"), "scheme": balancer.get("Scheme"),
            "vpc_id": balancer.get("VPCId"), "subnets": balancer.get("Subnets", []),
            "availability_zones": balancer.get("AvailabilityZones", []),
            "security_groups": balancer.get("SecurityGroups", []),
        }, seen_ids=seen_node_ids, seen_at=sync_started_at)
        for instance in balancer.get("Instances", []):
            instance_id = instance.get("InstanceId") if isinstance(instance, dict) else None
            if instance_id in instances:
                _upsert_edge(db, f"aws_instance.{instance_id}", identifier, "registered_with", inferred_from="elb_attachment", confidence=1.0)

    target_groups: dict[str, str] = {}
    for group in _page(elbv2, "describe_target_groups", "TargetGroups", warnings, "ELB target groups"):
        identifier = f"aws_lb_target_group.{group['TargetGroupArn'].rsplit('/', 1)[-1]}"
        target_groups[group["TargetGroupArn"]] = identifier
        _upsert_node(db, identifier, group["TargetGroupName"], "target_group", {}, {"arn": group["TargetGroupArn"]}, seen_ids=seen_node_ids, seen_at=sync_started_at)
        try:
            health = elbv2.describe_target_health(TargetGroupArn=group["TargetGroupArn"])
            for target in health.get("TargetHealthDescriptions", []):
                target_id = target["Target"]["Id"]
                if target_id in instances:
                    _upsert_edge(db, f"aws_instance.{target_id}", identifier, "registered_with")
        except (BotoCoreError, ClientError) as exc:
            warnings.append(f"Target health for {group['TargetGroupName']} skipped: {exc}")

    for balancer in _page(elbv2, "describe_load_balancers", "LoadBalancers", warnings, "Application Load Balancers"):
        identifier = f"aws_lb.{balancer['LoadBalancerArn'].rsplit('/', 1)[-1]}"
        availability_zones = balancer.get("AvailabilityZones", [])
        _upsert_node(db, identifier, balancer["LoadBalancerName"], "load_balancer", {}, {
            "arn": balancer["LoadBalancerArn"], "scheme": balancer["Scheme"],
            "type": balancer.get("Type", "application"), "state": (balancer.get("State") or {}).get("Code"),
            "vpc_id": balancer.get("VpcId"), "dns_name": balancer.get("DNSName"),
            "subnets": [zone.get("SubnetId") for zone in availability_zones if isinstance(zone, dict) and zone.get("SubnetId")],
            "security_groups": balancer.get("SecurityGroups", []),
        }, seen_ids=seen_node_ids, seen_at=sync_started_at)
        if balancer.get("Type", "application") != "application":
            continue
        for listener in _page(
            elbv2, "describe_listeners", "Listeners", warnings, "ELB listeners",
            LoadBalancerArn=balancer["LoadBalancerArn"],
        ):
            listener_arn = listener.get("ListenerArn")
            if not isinstance(listener_arn, str):
                warnings.append(f"Listener for {balancer['LoadBalancerName']} skipped: missing ListenerArn")
                continue
            listener_id = f"aws_lb_listener.{listener_arn.rsplit('/', 1)[-1]}"
            _upsert_node(db, listener_id, f"{balancer['LoadBalancerName']}:{listener.get('Port', 'unknown')}", "listener", {}, {
                "arn": listener_arn, "load_balancer_arn": balancer["LoadBalancerArn"],
                "protocol": listener.get("Protocol"), "port": listener.get("Port"),
            }, seen_ids=seen_node_ids, seen_at=sync_started_at)
            _upsert_edge(db, listener_id, identifier, "attached_to_lb", inferred_from="aws_listener", confidence=1.0)
            for index, action in enumerate(listener.get("DefaultActions", [])):
                if not isinstance(action, dict):
                    continue
                action_id = f"{listener_id}.default_action.{index}"
                action_type = str(action.get("Type", "unknown"))
                _upsert_node(db, action_id, f"{listener.get('Port', 'unknown')} {action_type}", "listener_action", {}, {
                    "listener_arn": listener_arn, "action_type": action_type, "order": index,
                }, seen_ids=seen_node_ids, seen_at=sync_started_at)
                _upsert_edge(db, action_id, listener_id, "attached_to_listener", inferred_from="aws_listener", confidence=1.0)
                for target_group_arn in _target_group_arns(action):
                    target_group_id = target_groups.get(target_group_arn)
                    if target_group_id is None:
                        warnings.append(f"Listener {listener_arn} references target group not returned by DescribeTargetGroups: {target_group_arn}")
                        continue
                    _upsert_edge(db, target_group_id, action_id, "used_by_action", inferred_from="aws_listener_action", confidence=1.0)

    for function in _page(lambdas, "list_functions", "Functions", warnings, "Lambda"):
        function_id = f"aws_lambda_function.{function['FunctionName']}"
        _upsert_node(db, function_id, function["FunctionName"], "lambda", {}, {"runtime": function.get("Runtime")}, seen_ids=seen_node_ids, seen_at=sync_started_at)
        values = function.get("Environment", {}).get("Variables", {}).values()
        for db_name, db_id in rds_ids.items():
            if any(db_name in str(value) for value in values):
                _upsert_edge(db, db_id, function_id, "configured_reference")

    try:
        buckets = s3.list_buckets().get("Buckets", [])
    except (BotoCoreError, ClientError) as exc:
        warnings.append(f"S3 skipped: {exc}")
        buckets = []
    for bucket in buckets:
        name = bucket.get("Name")
        if not isinstance(name, str):
            continue
        try:
            location = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except (BotoCoreError, ClientError) as exc:
            warnings.append(f"S3 bucket {name} location skipped: {exc}")
            continue
        if location == "EU":
            location = "eu-west-1"
        if location != settings.aws_region:
            continue
        tags: dict[str, str] = {}
        try:
            tags = _tags(s3.get_bucket_tagging(Bucket=name).get("TagSet", []))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "NoSuchTagSet":
                warnings.append(f"S3 bucket {name} tags skipped: {exc}")
        except BotoCoreError as exc:
            warnings.append(f"S3 bucket {name} tags skipped: {exc}")
        created_at = bucket.get("CreationDate")
        metadata = {
            "region": location,
            "creation_date": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
        }
        _upsert_node(
            db, f"aws_s3_bucket.{name}", name, "s3_bucket", tags, metadata,
            terraform_address=_terraform_address_from_tags("aws_s3_bucket", tags),
            seen_ids=seen_node_ids, seen_at=sync_started_at,
        )

    warnings = list(dict.fromkeys(warnings))
    stale_nodes_marked = 0
    # A partial sync must never make an access failure look like resource deletion.
    if not warnings:
        stale_nodes = list(db.scalars(select(ResourceNode).where(
            ResourceNode.region == settings.aws_region,
            ResourceNode.is_active.is_(True),
            ResourceNode.external_id.not_in(seen_node_ids),
        ))) if seen_node_ids else list(db.scalars(select(ResourceNode).where(
            ResourceNode.region == settings.aws_region, ResourceNode.is_active.is_(True),
        )))
        for node in stale_nodes:
            node.is_active = False
        stale_nodes_marked = len(stale_nodes)
    active_nodes = db.scalar(select(func.count()).select_from(ResourceNode).where(
        ResourceNode.is_active.is_(True), ResourceNode.region == settings.aws_region,
    )) or 0
    active_ids = set(db.scalars(select(ResourceNode.external_id).where(
        ResourceNode.is_active.is_(True), ResourceNode.region == settings.aws_region,
    )))
    active_edges = sum(1 for edge in db.scalars(select(DependencyEdge)) if edge.source_external_id in active_ids and edge.target_external_id in active_ids)
    db.add(TopologySync(
        account_id=account, region=settings.aws_region, status="partial" if warnings else "complete",
        active_nodes=active_nodes, active_edges=active_edges, stale_nodes_marked=stale_nodes_marked,
        warnings=warnings, completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return {"account_id": account, "region": settings.aws_region, "nodes": active_nodes,
            "edges": active_edges, "stale_nodes_marked": stale_nodes_marked, "warnings": warnings}


def stored_topology(db: Session) -> dict:
    nodes = [{"id": node.external_id, "name": node.name, "kind": node.resource_type, "tags": node.tags, "metadata": node.metadata_json,
              "last_seen_at": node.last_seen_at} for node in db.scalars(select(ResourceNode).where(
                  ResourceNode.is_active.is_(True),
                  ResourceNode.region == settings.aws_region,
              ).order_by(ResourceNode.resource_type, ResourceNode.name))]
    active_ids = {node["id"] for node in nodes}
    latest_sync = db.scalar(select(TopologySync).where(TopologySync.region == settings.aws_region).order_by(TopologySync.completed_at.desc()))
    inactive_nodes = db.scalar(select(func.count()).select_from(ResourceNode).where(
        ResourceNode.is_active.is_(False), ResourceNode.region == settings.aws_region,
    )) or 0
    return {"nodes": nodes,
            "edges": [{"source": edge.source_external_id, "target": edge.target_external_id, "type": edge.edge_type, "confidence": edge.confidence} for edge in db.scalars(select(DependencyEdge).order_by(DependencyEdge.source_external_id, DependencyEdge.target_external_id)) if edge.source_external_id in active_ids and edge.target_external_id in active_ids],
            "last_synchronized_at": latest_sync.completed_at if latest_sync else None,
            "sync_status": latest_sync.status if latest_sync else None,
            "inactive_nodes": inactive_nodes}
