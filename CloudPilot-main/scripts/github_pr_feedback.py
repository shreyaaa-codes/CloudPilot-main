#!/usr/bin/env python3
"""Submit a sanitized Terraform plan to CloudPilot from a GitHub Actions job."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--environment", default="staging")
    parser.add_argument("--team", default="unassigned")
    parser.add_argument("--remaining-budget", type=float, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = {"plan": json.loads(args.plan.read_text()), "context": {
        "project": args.project, "pull_request": args.pull_request, "environment": args.environment,
        "team": args.team, "remaining_budget": args.remaining_budget, "actor": "github-actions",
    }}
    request = Request(
        f"{args.api_url.rstrip('/')}/api/analyses", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        raise SystemExit(f"CloudPilot API returned HTTP {error.code}; response is intentionally not logged.") from error
    except URLError as error:
        raise SystemExit(f"CloudPilot API could not be reached: {error.reason}") from error
    args.output.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
