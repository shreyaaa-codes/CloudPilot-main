"""Read-only AWS observations used by the explicit V3 verification workflow."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import settings


class CloudWatchTelemetryError(RuntimeError):
    pass


class CostExplorerObservationError(RuntimeError):
    pass


def ec2_cpu_observation(instance_id: str, window_minutes: int) -> dict:
    """Return aggregate EC2 CPU utilisation; it never changes AWS resources."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    try:
        session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        response = session.client("cloudwatch", region_name=settings.aws_region).get_metric_statistics(
            Namespace="AWS/EC2", MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start, EndTime=end, Period=300, Statistics=["Average", "Minimum", "Maximum"],
        )
    except (BotoCoreError, ClientError) as exc:
        raise CloudWatchTelemetryError(str(exc)) from exc
    points = response.get("Datapoints", [])
    averages = [point["Average"] for point in points if "Average" in point]
    minimums = [point["Minimum"] for point in points if "Minimum" in point]
    maximums = [point["Maximum"] for point in points if "Maximum" in point]
    return {
        "source": "AWS/EC2 CPUUtilization", "instance_id": instance_id, "region": settings.aws_region,
        "window_minutes": window_minutes, "datapoints": len(points),
        "average_percent": round(sum(averages) / len(averages), 2) if averages else None,
        "minimum_percent": round(min(minimums), 2) if minimums else None,
        "maximum_percent": round(max(maximums), 2) if maximums else None,
        "observed_at": end.isoformat(),
    }


def cost_explorer_observation(start_date: date, end_date: date, service: str | None = None) -> dict:
    """Read an account or AWS-service period cost from Cost Explorer.

    AWS Cost Explorer's end date is exclusive. This function intentionally
    returns the billing-period total as a signal, not a per-resource or
    deployment-attributed cost. The reviewer remains responsible for entering
    the observed change delta used by verification.
    """
    if end_date <= start_date:
        raise CostExplorerObservationError("end_date must be after start_date (Cost Explorer end dates are exclusive).")
    try:
        session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.cost_explorer_region)
        request: dict = {
            "TimePeriod": {"Start": start_date.isoformat(), "End": end_date.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
        }
        if service:
            request["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": [service]}}
        response = session.client("ce", region_name=settings.cost_explorer_region).get_cost_and_usage(**request)
    except (BotoCoreError, ClientError) as exc:
        raise CostExplorerObservationError(str(exc)) from exc
    total = sum(
        float(day.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0) or 0)
        for day in response.get("ResultsByTime", [])
    )
    return {
        "source": "AWS Cost Explorer / UnblendedCost",
        "scope": "service" if service else "account",
        "service": service,
        "start_date": start_date.isoformat(),
        "end_date_exclusive": end_date.isoformat(),
        "currency": "USD",
        "amount": round(total, 2),
        "periods": len(response.get("ResultsByTime", [])),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "attribution_limitation": "This is account or AWS-service period cost, not per-resource or per-deployment attribution.",
    }
