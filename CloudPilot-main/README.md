# CloudPilot

**CloudPilot: An Explainable Pre-Deployment Decision Framework for Infrastructure Cost and Risk Analysis**

CloudPilot is an AWS-focused infrastructure change-intelligence platform. It reviews a Terraform plan before deployment, combines estimated cost with business policy and live dependency impact, and returns an explainable `ALLOW`, `WARN`, or `BLOCK` decision. After deployment, it records observed outcomes and evaluates the accuracy of its earlier prediction.

CloudPilot does not replace AWS, Infracost, env0, or Spacelift. AWS provides many of the underlying capabilities—Cost Explorer, CloudWatch, IAM, CloudFormation controls, and governance primitives. CloudPilot’s focus is a single, change-centric decision workflow that combines those signals with organization-specific context.

## Architecture

```text
GitHub pull request
        |
GitHub Actions -> terraform plan -> terraform show -json
        |                                  |
        +----------------------------> CloudPilot API
                                             |
     +----------------------+----------------+---------------------+
     |                      |                |                     |
Terraform parser      Static cost engine  Policy/business      Live AWS topology
     |                      |              context database     (read-only discovery)
     +----------------------+----------------+                     |
                                             |                 BFS / DFS
                                     Composite risk engine          |
                                             |                  Blast radius
                                   ALLOW / WARN / BLOCK             |
                                             |                       |
                                    GitHub PR feedback <-------------+
                                             |
                                        deployment
                                             |
                     observed cost + CloudWatch telemetry + observed dependencies
                                             |
                                  predicted-versus-actual verification
                                             |
                           historical prediction confidence and audit history
```

See [docs/architecture.md](docs/architecture.md) for component-level data flow.

## Capability progression

| Version | Delivered capability |
| --- | --- |
| V1 | Terraform normalization, transparent EC2/RDS/EBS estimate, policy checks, decision, audit record, GitHub-ready comment. |
| V2 | Persistent business context, read-only AWS discovery, listener-derived ALB relationships, persisted topology, BFS/DFS blast radius, configurable criticality, maintenance-window-aware risk. |
| V3 | Deployment identifier, CI prediction artifact, observed cost/telemetry/dependencies, Cost Explorer signal, prediction accuracy, precision/recall/F1, health outcome, and historical prediction confidence. |

## Core workflow

```text
Terraform plan -> CloudPilot analysis -> decision -> deployment -> observed outcome -> verification
```

1. CloudPilot reads `terraform show -json` output.
2. It normalizes supported managed EC2, RDS, and EBS creates, updates, deletes, and replacements.
3. It calculates static monthly estimates and database-backed policy/business context.
4. It traverses the active persisted AWS topology with BFS and DFS.
5. It produces an explainable composite risk and decision.
6. After a real deployment, a reviewer or CI process associates the retained analysis ID with a deployment identifier and records observed cost, affected resources, and telemetry.
7. CloudPilot compares the prediction with the observation and updates historical confidence.

## Composite risk heuristic

CloudPilot’s score is an explainable heuristic, not a claim of objectively true risk or an ML prediction.

| Dimension | Maximum | Calculation |
| --- | ---: | --- |
| Cost impact | 25 | 0–12 points for absolute positive change plus 0–13 based on positive delta / remaining team budget. |
| Dependency radius | 25 | Direct, indirect, and distinct affected Tier-1 service counts. |
| Service criticality | 25 | Highest database-configured service criticality for a changed resource. |
| Change magnitude | 15 | Replace/delete > update > create. |
| Maintenance timing | 10 | Production impact outside an approved maintenance window. |

Every component and its inputs are retained in the analysis report and shown in the Change Detail view.

## Topology and graph semantics

An edge `A -> B` means: **a change to A may affect B**. Production analysis only uses active nodes from the persisted live topology; [topology/default.json](topology/default.json) is a deterministic fixture used only by tests.

Supported read-only AWS discovery:

- EC2 instances and EBS attachments
- RDS instances
- Auto Scaling Groups and instances
- Application Load Balancer -> listener -> listener action -> target group relationships
- Target group registrations
- Lambda functions that explicitly reference discovered RDS names in environment variables

Discovery records the time of each synchronization. Successful complete syncs mark absent resources inactive; inactive resources and dangling edges are excluded from live risk analysis. A partial sync with AWS warnings never treats missing results as deletions.

## V3 prediction verification

One verification record is allowed per analysis. It contains a deployment/change identifier, observed monthly cost delta, observed affected IDs, and optional before/after health telemetry.

- Cost error: `abs(actual - predicted) / max(abs(predicted), 1)`.
- Dependency precision: correctly predicted / predicted dependencies.
- Dependency recall: correctly predicted / observed dependencies.
- F1: harmonic mean of precision and recall.
- Health: `HEALTHY`, `DEGRADED`, `CRITICAL`, or `NOT_OBSERVED` using documented latency, error-rate, and availability thresholds in the code.
- Historical Prediction Confidence: `100 - mean historical cost error` for verified changes with overlapping Terraform resource types. The neutral prior is 75% with zero observations; it is not formal statistical confidence.

The optional Cost Explorer read returns account- or AWS-service-period `UnblendedCost`; it is retained with the verification as supporting evidence. AWS billing data has timing delays and is not reliably attributable to a single Terraform resource or deployment. A reviewer/approved automation still supplies the delta that is compared to the prediction.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. The first start creates the additive schema and deterministic demo context (Payments team, budget, criticality, mapping, and default policy). SQLite is the local default; set `DATABASE_URL` to a PostgreSQL-compatible URL for deployment.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | `sqlite:///./cloudpilot.db` locally, or PostgreSQL SQLAlchemy URL. |
| `AWS_REGION` | Region used for optional AWS topology and CloudWatch reads. |
| `AWS_PROFILE` | Local named AWS profile; never commit its credentials. |
| `AWS_COST_EXPLORER_REGION` | Cost Explorer endpoint, defaulting to `us-east-1`. |
| `APP_ENV` / `SEED_DEMO_DATA` | Set `APP_ENV=production` (or `SEED_DEMO_DATA=false`) to prevent example business context from being inserted. |
| `CORS_ORIGINS` | Optional comma-separated origins for a separately hosted frontend. Omit for same-origin local use. |

### IAM

Attach [policies/live-topology-readonly.json](policies/live-topology-readonly.json) to a least-privilege IAM user or role. It permits only identity lookup, supported `Describe`/`List` calls, `cloudwatch:GetMetricStatistics`, and `ce:GetCostAndUsage`. CloudPilot does not create, update, invoke, stop, or delete AWS resources.

## API

FastAPI documentation is available at `/docs`.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/analyses` | Analyze Terraform `show -json` data. |
| `GET /api/analyses`, `GET /api/analyses/{id}` | Retrieve review history/detail. |
| `GET /api/analyses/{id}/github-comment` | Get Markdown PR feedback. |
| `GET /api/topology`, `POST /api/topology/sync` | View/sync active read-only AWS topology. |
| `POST /api/analyses/{id}/verify`, `GET /api/verifications` | Record and inspect prediction verification. |
| `POST /api/telemetry/ec2-cpu` | Explicit read-only EC2 CloudWatch CPU observation. |
| `POST /api/telemetry/cost-explorer` | Explicit account/service-period Cost Explorer signal. |

## GitHub pull-request workflow

[.github/workflows/cloudpilot-pr.yml](.github/workflows/cloudpilot-pr.yml) supplies an actual PR gate.

1. Configure the `CLOUDPILOT_API_URL` GitHub Actions secret with a reachable deployed CloudPilot URL.
2. Optionally configure `CLOUDPILOT_TF_DIRECTORY`, `CLOUDPILOT_ENVIRONMENT`, `CLOUDPILOT_TEAM`, and `CLOUDPILOT_REMAINING_BUDGET` as repository variables.
3. The workflow runs Terraform in the configured directory, or uses the deterministic safe fixture if none is configured.
4. It submits plan JSON without writing plan contents to logs, posts a concise PR comment, retains `cloudpilot-result.json` as a run-specific artifact, and fails for `BLOCK`.

The result artifact contains the authoritative CloudPilot analysis ID. A deployment workflow downloads that artifact and can run:

```bash
python scripts/verify_deployment.py --api-url "$CLOUDPILOT_API_URL" \
  --analysis-result cloudpilot-result.json --deployment-identifier "$DEPLOYMENT_ID" \
  --actual-monthly-cost-delta 24.50 --observed-resource aws_instance.example
```

That makes the prediction-to-deployment association explicit and prevents a deployment identifier from being reused for a different analysis.

GitHub-hosted runners cannot reach `localhost`; deploy CloudPilot to a reachable private/public environment or use a self-hosted runner. Store URLs/tokens only as GitHub secrets—never in the repository.

## Deterministic end-to-end demonstration

1. Start CloudPilot and open the dashboard.
2. Click **Analyze sample plan**. It changes production RDS from `db.t3.medium` to `db.r6g.2xlarge`; the seeded Payments budget and criticality produce a clear `BLOCK` decision.
3. In AWS, configure `.env` with your named read-only profile and region, then click **Sync read-only AWS topology**. The dashboard shows only actual active discovered resources and a freshness timestamp.
4. Select the new review’s **View impact** button. Inspect the change detail, risk dimensions, policy reasons, BFS/DFS paths, and interactive impact graph.
5. For a real deployed change, select the analysis under **Record a deployed change outcome**, enter a deployment identifier, actual cost signal, and observed affected IDs. Optionally attach EC2 CPU and Cost Explorer signals. Submit verification.
6. Inspect precision, recall, F1, health outcome, and historical confidence in the verification panel.

## Security and data handling

- No AWS or GitHub credentials are stored in source or frontend code.
- Terraform `before`/`after` values are redacted before persistence when Terraform marks them sensitive or their keys indicate common secrets.
- AWS discovery and telemetry calls are read-only.
- AWS failures produce safe warnings; a partial sync does not deactivate existing topology.
- CORS is opt-in and restricted to configured origins.

## Testing

```bash
source .venv/bin/activate
python -m pytest -q
```

The suite covers parser action/malformed cases, static cost calculation, database business context, maintenance windows, live topology persistence/listener relationships/staleness, BFS/DFS and Tier-1 coverage, risk bounds and contextual cost, API workflow, V3 verification metrics, secret redaction, and CloudWatch/Cost Explorer adapter behavior.

## Limitations and future scope

- Static pricing covers only EC2, RDS, and EBS and is an estimate, not live AWS billing.
- Topology discovery intentionally covers a bounded set of AWS relationships; it does not claim to model every AWS dependency.
- V3 observation is associated through a reviewer/CI-provided deployment identifier. It is not automatic deployment control or rollback.
- CloudWatch collection currently has an explicit EC2 CPU path; latency/error/availability inputs must come from appropriate approved telemetry sources. Cost Explorer is an account/service-period signal only, never asserted as direct deployment or resource attribution.
- No V4/multi-cloud support is implemented.

## GitHub PR Demo Commands

Use these exact commands during the review to demonstrate the GitHub integration cleanly.

### ALLOW PR

1. Setup the branch:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b demo/allow-upgrade
   ```
2. Make a small, safe change in `terraform/demo/main.tf` (e.g., `t3.micro` → `t3.small`).
3. Commit and push:
   ```bash
   git add terraform/demo/main.tf
   git commit -m "Demo: upgrade EC2 instance"
   git push -u origin demo/allow-upgrade
   ```
4. Open the PR on GitHub (`demo/allow-upgrade` → `main`) and watch the CloudPilot Action pass.

### BLOCK PR

1. After you’re done with the first PR, create the second branch:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b demo/block-change
   ```
2. Make an expensive/risky change in `terraform/demo/main.tf` (e.g., changing an RDS instance class to `db.r6g.2xlarge`).
3. Commit and push:
   ```bash
   git add .
   git commit -m "Demo: high risk infrastructure change"
   git push -u origin demo/block-change
   ```
4. Open the PR on GitHub (`demo/block-change` → `main`) and watch the CloudPilot Action correctly fail the gate (BLOCK).

### Modifying an open PR

If you need to tweak something while the PR is still open:
```bash
git add .
git commit -m "Fix demo"
git push
```
