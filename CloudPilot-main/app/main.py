"""FastAPI entry point for CloudPilot's AWS change-intelligence workflow."""
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .business_context import initialize_database
from .config import settings
from .database import get_db
from .cloudwatch import CloudWatchTelemetryError, CostExplorerObservationError, cost_explorer_observation, ec2_cpu_observation
from .models import AuditLog, ChangeAnalysis
from .live_topology import TopologyDiscoveryError, discover_live_topology, stored_topology
from .schemas import AnalysisResponse, AuditEntry, CostExplorerTelemetryRequest, DashboardSummary, Ec2CpuTelemetryRequest, PlanSubmission, VerificationResponse, VerificationSubmission
from .services import github_comment, response_for, submit_plan
from .verification import response_for_verification, verification_history, verify_analysis

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    yield

app = FastAPI(title="CloudPilot", version="3.0.0", description="Explainable AWS infrastructure change intelligence", lifespan=lifespan)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
        allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", include_in_schema=False)
def dashboard(): return FileResponse(static_dir / "index.html")

@app.post("/api/analyses", response_model=AnalysisResponse, status_code=201)
def create_analysis(submission: PlanSubmission, db: Session = Depends(get_db)):
    return response_for(submit_plan(db, submission.plan, submission.context.model_dump(), set(submission.context.model_fields_set)))

@app.get("/api/analyses", response_model=list[AnalysisResponse])
def list_analyses(db: Session = Depends(get_db)):
    return [response_for(record) for record in db.scalars(select(ChangeAnalysis).order_by(ChangeAnalysis.created_at.desc()).limit(50))]

@app.get("/api/analyses/{analysis_id}", response_model=AnalysisResponse)
def get_analysis(analysis_id: int, db: Session = Depends(get_db)):
    record = db.get(ChangeAnalysis, analysis_id)
    if record is None: raise HTTPException(404, "Analysis not found")
    return response_for(record)

@app.get("/api/analyses/{analysis_id}/github-comment", response_class=PlainTextResponse)
def get_github_comment(analysis_id: int, db: Session = Depends(get_db)):
    record = db.get(ChangeAnalysis, analysis_id)
    if record is None: raise HTTPException(404, "Analysis not found")
    return github_comment(record)

@app.get("/api/summary", response_model=DashboardSummary)
def summary(db: Session = Depends(get_db)):
    counts = {decision: db.scalar(select(func.count()).select_from(ChangeAnalysis).where(ChangeAnalysis.decision == decision)) or 0 for decision in ("ALLOW", "WARN", "BLOCK")}
    return DashboardSummary(total_changes=sum(counts.values()), allowed=counts["ALLOW"], warnings=counts["WARN"], blocked=counts["BLOCK"], monthly_cost_delta=round(db.scalar(select(func.coalesce(func.sum(ChangeAnalysis.monthly_cost_delta), 0))) or 0, 2))

@app.get("/api/audit", response_model=list[AuditEntry])
def audit_log(db: Session = Depends(get_db)):
    return [AuditEntry(event=row.event, actor=row.actor, details=row.details, created_at=row.created_at) for row in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(50))]

@app.get("/api/topology")
def topology(db: Session = Depends(get_db)):
    """Return stored live-discovery nodes and edges for the V2 graph view."""
    return stored_topology(db)

@app.post("/api/topology/sync")
def sync_topology(db: Session = Depends(get_db)):
    """Opt-in, read-only AWS discovery. It never starts, stops, or edits resources."""
    try:
        result = discover_live_topology(db)
    except TopologyDiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc
    db.add(AuditLog(analysis_id=0, actor="topology-sync", event="live_topology_discovered", details=result))
    db.commit()
    return result


@app.post("/api/analyses/{analysis_id}/verify", response_model=VerificationResponse, status_code=201)
def verify_prediction(analysis_id: int, submission: VerificationSubmission, db: Session = Depends(get_db)):
    """Record post-deployment observations and calculate V3 prediction accuracy."""
    record = db.get(ChangeAnalysis, analysis_id)
    if record is None:
        raise HTTPException(404, "Analysis not found")
    try:
        result = verify_analysis(db, record, submission.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return response_for_verification(result, record)


@app.get("/api/verifications", response_model=list[VerificationResponse])
def list_verifications(db: Session = Depends(get_db)):
    return verification_history(db)


@app.post("/api/telemetry/ec2-cpu")
def read_ec2_cpu_telemetry(request: Ec2CpuTelemetryRequest):
    """Opt-in V3 CloudWatch read for an EC2 instance selected by the reviewer."""
    try:
        return ec2_cpu_observation(request.instance_id, request.window_minutes)
    except CloudWatchTelemetryError as exc:
        raise HTTPException(502, f"CloudWatch read failed: {exc}") from exc


@app.post("/api/telemetry/cost-explorer")
def read_cost_explorer_telemetry(request: CostExplorerTelemetryRequest):
    """Opt-in read of account/service-period cost; never claims deployment attribution."""
    try:
        return cost_explorer_observation(request.start_date, request.end_date, request.service)
    except CostExplorerObservationError as exc:
        raise HTTPException(502, f"Cost Explorer read failed: {exc}") from exc
