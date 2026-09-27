# CloudPilot architecture and data flow

## Components

| Component | Responsibility | Storage / dependency |
| --- | --- | --- |
| FastAPI API | Receives plans, serves dashboard/API, validates request bodies. | SQLAlchemy database |
| Terraform parser | Converts `resource_changes` into supported, normalized managed EC2/RDS/EBS changes. | Pure Python |
| Cost and policy engines | Produces static estimates and deterministic policy findings. | Versioned price catalogue + persisted policy configuration |
| Business context | Resolves teams, budgets, service-resource mappings, criticality, and maintenance windows. | Database |
| AWS discovery | Discovers a bounded topology using only read-only AWS calls. | boto3 + resource/edge tables |
| Graph engine | Runs depth-limited BFS and DFS on active persisted topology. | Database topology source |
| Risk engine | Combines cost/budget, graph, criticality, magnitude, and timing into 0–100 heuristic. | Analysis report |
| Verification engine | Compares original prediction and post-deployment observation. | Verification and audit records |
| AWS observation adapters | Explicit read-only EC2 CPU and account/service Cost Explorer signals. | CloudWatch / Cost Explorer |

## Pre-deployment data flow

```text
terraform show -json
      |
POST /api/analyses
      |
Parser -> normalized changes -> Cost engine -> Policy engine
      |                                  |
      +------> Business context <--------+
                      |
Persisted active AWS topology -> BFS / DFS -> blast radius
                      |
                 Risk + decision
                      |
        ChangeAnalysis + AuditLog + PR Markdown
```

## AWS topology flow

```text
AWS read-only APIs -> normalized ResourceNode / DependencyEdge -> active graph
                                  |
                         TopologySync timestamp/status
                                  |
                     DatabaseTopologySource -> graph engine
```

A complete sync marks old regional nodes inactive only if they were absent from a successful response set. A partial sync retains the old active graph. Graph traversal ignores inactive nodes and any resulting dangling edges.

## V3 verification flow

```text
ChangeAnalysis (prediction before deployment; CI retains its result artifact)
                |
deployment identifier + observed cost + observed IDs + telemetry
                |
        VerificationResult + AuditLog
                |
cost error / precision / recall / F1 / health / historical confidence
```

The verification is intentionally not an automated deployment or rollback system. An explicit EC2 CPU observation can be collected from CloudWatch, and Cost Explorer can provide account/service-period `UnblendedCost`. Cost Explorer is deliberately not interpreted as per-resource or deployment attribution; reviewer/approved automation supplies the verified delta.
