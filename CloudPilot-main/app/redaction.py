"""Redact Terraform values before they enter persistent reports or audit storage."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


SENSITIVE_KEYWORDS = ("password", "secret", "token", "private_key", "access_key", "api_key")
REDACTED = "[REDACTED]"


def redact_value(value: Any, sensitive_shape: Any = None, key: str = "") -> Any:
    """Honor Terraform's *_sensitive masks and conservatively redact sensitive keys."""
    if sensitive_shape is True or any(keyword in key.lower() for keyword in SENSITIVE_KEYWORDS):
        return REDACTED
    if isinstance(value, dict):
        mask = sensitive_shape if isinstance(sensitive_shape, dict) else {}
        return {name: redact_value(item, mask.get(name), name) for name, item in value.items()}
    if isinstance(value, list):
        mask = sensitive_shape if isinstance(sensitive_shape, list) else []
        return [redact_value(item, mask[index] if index < len(mask) else None, key) for index, item in enumerate(value)]
    return value


def redact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a safe copy of terraform show -json data for persistence."""
    safe = deepcopy(plan)
    for resource in safe.get("resource_changes", []) if isinstance(safe.get("resource_changes"), list) else []:
        if not isinstance(resource, dict) or not isinstance(resource.get("change"), dict):
            continue
        change = resource["change"]
        for side in ("before", "after"):
            if isinstance(change.get(side), (dict, list)):
                change[side] = redact_value(change[side], change.get(f"{side}_sensitive"))
    return safe


def redact_report(report: dict[str, Any], original_plan: dict[str, Any]) -> dict[str, Any]:
    """Apply the same masks to normalized changes retained in an analysis report."""
    safe_report = deepcopy(report)
    masks: dict[str, dict[str, Any]] = {}
    for resource in original_plan.get("resource_changes", []) if isinstance(original_plan.get("resource_changes"), list) else []:
        if isinstance(resource, dict) and isinstance(resource.get("address"), str) and isinstance(resource.get("change"), dict):
            masks[resource["address"]] = resource["change"]
    for change in safe_report.get("changes", []):
        if not isinstance(change, dict):
            continue
        original_change = masks.get(change.get("address"), {})
        before = redact_value(change.get("before"), original_change.get("before_sensitive"))
        after = redact_value(change.get("after"), original_change.get("after_sensitive"))
        # ChangeSummary intentionally guarantees object-shaped before/after
        # values. Terraform may mark an entire object sensitive, so preserve
        # that response contract without exposing any part of the object.
        change["before"] = before if isinstance(before, dict) else {"_sensitive": REDACTED}
        change["after"] = after if isinstance(after, dict) else {"_sensitive": REDACTED}
    return safe_report
