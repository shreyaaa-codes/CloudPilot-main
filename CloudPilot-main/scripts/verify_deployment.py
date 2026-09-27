#!/usr/bin/env python3
"""Associate a deployment outcome with the CloudPilot analysis saved by CI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--analysis-result", type=Path, required=True, help="cloudpilot-result.json artifact from the prediction workflow")
    parser.add_argument("--deployment-identifier", required=True)
    parser.add_argument("--actual-monthly-cost-delta", required=True, type=float)
    parser.add_argument("--observed-resource", action="append", default=[])
    parser.add_argument("--telemetry", type=Path, help="Optional JSON object with approved health observations")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = json.loads(args.analysis_result.read_text())
    analysis_id = result.get("id")
    if not isinstance(analysis_id, int):
        raise SystemExit("The analysis artifact does not contain a valid CloudPilot analysis id.")
    telemetry = json.loads(args.telemetry.read_text()) if args.telemetry else {}
    if not isinstance(telemetry, dict):
        raise SystemExit("Telemetry JSON must be an object.")
    payload = {
        "deployment_identifier": args.deployment_identifier,
        "actual_monthly_cost_delta": args.actual_monthly_cost_delta,
        "observed_affected_resources": args.observed_resource,
        "telemetry": telemetry,
        "actor": "deployment-automation",
    }
    request = Request(
        f"{args.api_url.rstrip('/')}/api/analyses/{analysis_id}/verify",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            verification = json.loads(response.read())
    except HTTPError as error:
        raise SystemExit(f"CloudPilot verification returned HTTP {error.code}; response is intentionally not logged.") from error
    except URLError as error:
        raise SystemExit(f"CloudPilot verification API could not be reached: {error.reason}") from error
    if args.output:
        args.output.write_text(json.dumps(verification))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
