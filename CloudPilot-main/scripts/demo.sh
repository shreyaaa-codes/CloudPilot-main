#!/usr/bin/env bash
# =============================================================================
#  CloudPilot Interactive Demo
#  Usage: ./scripts/demo.sh
# =============================================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_CYAN='\033[36m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_RED='\033[31m'
C_WHITE='\033[97m'

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$ROOT/terraform/demo"
PLAN_JSON="$TF_DIR/tfplan.json"
API="http://127.0.0.1:8000"

# ── Helpers ───────────────────────────────────────────────────────────────────
print_header() {
  clear
  echo -e "${C_CYAN}${C_BOLD}"
  echo "  ╭──────────────────────────────────────────────╮"
  echo "  │                                              │"
  echo "  │          ☁  CLOUDPILOT DEMO ☁              │"
  echo "  │   AWS Change Intelligence & Governance       │"
  echo "  │                                              │"
  echo "  ╰──────────────────────────────────────────────╯"
  echo -e "${C_RESET}"
}

print_menu() {
  echo -e "${C_WHITE}${C_BOLD}  What would you like to do?${C_RESET}\n"
  echo -e "  ${C_CYAN}1.${C_RESET}  Check AWS connection"
  echo -e "  ${C_CYAN}2.${C_RESET}  Discover live AWS topology"
  echo -e "  ${C_CYAN}3.${C_RESET}  Generate Terraform plan"
  echo -e "  ${C_CYAN}4.${C_RESET}  Analyze Terraform change"
  echo -e "  ${C_CYAN}5.${C_RESET}  View latest analysis"
  echo -e "  ${C_CYAN}6.${C_RESET}  Record deployment verification"
  echo -e "  ${C_GREEN}7.${C_RESET}  ${C_GREEN}${C_BOLD}Run complete demo (steps 1 → 5)${C_RESET}"
  echo -e "  ${C_DIM}8.  Exit${C_RESET}"
  echo
  echo -ne "  ${C_BOLD}Select an option:${C_RESET} "
}

step()    { echo -e "\n${C_CYAN}${C_BOLD}  ┌─ $1${C_RESET}"; }
ok()      { echo -e "  ${C_GREEN}✓${C_RESET}  $1"; }
warn()    { echo -e "  ${C_YELLOW}⚠${C_RESET}  $1"; }
err()     { echo -e "  ${C_RED}✗${C_RESET}  $1"; }
info()    { echo -e "  ${C_DIM}  $1${C_RESET}"; }
divider() { echo -e "  ${C_DIM}  ─────────────────────────────────────${C_RESET}"; }
pause()   { echo; echo -ne "  ${C_DIM}Press Enter to continue...${C_RESET}"; read -r; }

require_server() {
  if ! curl -sf "$API/" >/dev/null 2>&1; then
    echo
    err "CloudPilot server is not running."
    info "Start it with:  uvicorn app.main:app --reload"
    info "Then re-run:    ./scripts/demo.sh"
    echo
    exit 1
  fi
}

# ── 1. AWS Connection ─────────────────────────────────────────────────────────
check_aws() {
  step "1 / Checking AWS connection"
  if ! command -v aws &>/dev/null; then
    err "AWS CLI not found. Install from https://aws.amazon.com/cli/"; return 1
  fi

  IDENTITY=$(aws sts get-caller-identity --output json 2>&1) || {
    err "AWS credentials not configured or expired."
    info "Run: aws configure   (or export AWS_PROFILE=<profile>)"
    return 1
  }

  ACCOUNT=$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])"  2>/dev/null || echo "unknown")
  ARN=$(echo "$IDENTITY"     | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])"      2>/dev/null || echo "unknown")
  REGION=$(aws configure get region 2>/dev/null || echo "not set")

  divider
  ok "Connected to AWS"
  info "Account : $ACCOUNT"
  info "Identity: $ARN"
  info "Region  : $REGION"
  divider
}

# ── 2. Topology sync ──────────────────────────────────────────────────────────
discover_topology() {
  step "2 / Discovering live AWS topology  ${C_DIM}(read-only)${C_RESET}"
  require_server

  info "Calling POST $API/api/topology/sync …"
  RESP=$(curl -sf -X POST "$API/api/topology/sync" -H "Content-Type: application/json" 2>&1) || {
    err "Topology sync failed."; info "$RESP"; return 1
  }

  NODES=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('nodes','?'))" 2>/dev/null || echo "?")
  EDGES=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('edges','?'))" 2>/dev/null || echo "?")
  ACCT=$(echo "$RESP"  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('account_id',''))" 2>/dev/null || echo "")
  RGN=$(echo "$RESP"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('region',''))" 2>/dev/null || echo "")

  divider
  ok "Topology sync complete"
  info "Account : $ACCT  Region : $RGN"
  info "Nodes   : $NODES   Edges : $EDGES"
  info "View    : $API  → Topology section"
  divider
}

# ── 3. Terraform plan ─────────────────────────────────────────────────────────
generate_plan() {
  step "3 / Generating Terraform plan  ${C_DIM}(read-only, no apply)${C_RESET}"

  if ! command -v terraform &>/dev/null; then
    warn "Terraform CLI not found — falling back to pre-generated plan."
    if [ -f "$PLAN_JSON" ]; then
      ok "Using pre-generated plan: $PLAN_JSON  ($(wc -c < "$PLAN_JSON" | tr -d ' ') bytes)"
    else
      err "No pre-generated plan found at $PLAN_JSON"; return 1
    fi
    return 0
  fi

  info "terraform init …"
  terraform -chdir="$TF_DIR" init -input=false -upgrade -no-color >/dev/null 2>&1 && ok "Init complete" || warn "Init had warnings (continuing)"

  info "terraform plan …"
  if terraform -chdir="$TF_DIR" plan -out="$TF_DIR/tfplan" -input=false -no-color >/dev/null 2>&1; then
    ok "Plan generated"
    info "terraform show -json …"
    terraform -chdir="$TF_DIR" show -json "$TF_DIR/tfplan" > "$PLAN_JSON"
    ok "Plan exported → $PLAN_JSON"
  else
    warn "terraform plan exited non-zero — using pre-generated plan."
    [ -f "$PLAN_JSON" ] || { err "No fallback available."; return 1; }
  fi
  divider
  info "Plan file: $PLAN_JSON"
  divider
}

# ── 4. Analyze ────────────────────────────────────────────────────────────────
analyze_plan() {
  step "4 / Analyzing Terraform change"
  require_server

  echo -e "  ${C_DIM}Which plan would you like to analyze?${C_RESET}"
  echo -e "  ${C_CYAN}1.${C_RESET} Real AWS plan (aws_instance.cloudpilot_demo)"
  echo -e "  ${C_CYAN}2.${C_RESET} Multi-depth demo plan (aws_db_instance.checkout)"
  echo -ne "  ${C_BOLD}Select plan [1]:${C_RESET} "
  read -r PLAN_CHOICE
  
  if [[ "$PLAN_CHOICE" == "2" ]]; then
    PLAN_TO_USE="$ROOT/terraform/test-plans/multidepth-demo.json"
    [ -f "$PLAN_TO_USE" ] || { err "Multi-depth plan not found at $PLAN_TO_USE."; return 1; }
  else
    PLAN_TO_USE="$PLAN_JSON"
    [ -f "$PLAN_TO_USE" ] || { err "No plan JSON at $PLAN_TO_USE — run option 3 first."; return 1; }
  fi

  PAYLOAD=$(python3 - "$PLAN_TO_USE" "$PLAN_CHOICE" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    plan = json.load(f)
use_fixture = sys.argv[2] == "2"
payload = {
  "plan": plan,
  "context": {
    "project":                   "cloudpilot-demo",
    "pull_request":              "1",
    "environment":               "development",
    "team":                      "CloudPilot",
    "remaining_budget":          500,
    "maintenance_window_active": False,
    "use_fixture_topology":      use_fixture
  }
}
print(json.dumps(payload))
PYEOF
) || { err "Failed to build request payload."; return 1; }

  info "POST $API/api/analyses …"
  RESP=$(curl -sf -X POST "$API/api/analyses" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>&1) || {
    err "Analysis request failed."; info "$RESP"; return 1
  }

  ID=$(echo "$RESP"       | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','?'))"                                    2>/dev/null || echo "?")
  DECISION=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('decision','?'))"                                2>/dev/null || echo "?")
  RISK=$(echo "$RESP"     | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('risk',{}).get('score','?'))"                    2>/dev/null || echo "?")
  RISK_LABEL=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('risk',{}).get('label',''))"                   2>/dev/null || echo "")
  COST=$(echo "$RESP"     | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('monthly_cost_delta','?'))"                      2>/dev/null || echo "?")

  divider
  case "$DECISION" in
    ALLOW) echo -e "  ${C_GREEN}${C_BOLD}  ✓  DECISION: ALLOW${C_RESET}" ;;
    WARN)  echo -e "  ${C_YELLOW}${C_BOLD}  ⚠  DECISION: WARN${C_RESET}"  ;;
    BLOCK) echo -e "  ${C_RED}${C_BOLD}  ✗  DECISION: BLOCK${C_RESET}"    ;;
    *)     echo -e "  ${C_WHITE}${C_BOLD}  ?  DECISION: $DECISION${C_RESET}" ;;
  esac
  echo
  info "Analysis ID       : $ID"
  info "Risk score        : $RISK / 100  ${RISK_LABEL}"
  info "Monthly cost delta: \$$COST"
  info "Dashboard         : $API  → Change Detail & Impact"
  divider

  echo "$ID" > /tmp/.cloudpilot_last_analysis_id
}

# ── 5. View latest ────────────────────────────────────────────────────────────
view_latest() {
  step "5 / Latest analyses"
  require_server

  RESP=$(curl -sf "$API/api/analyses" 2>&1) || { err "Could not fetch analyses."; return 1; }
  COUNT=$(echo "$RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

  if [ "$COUNT" -eq 0 ]; then
    warn "No analyses found yet. Run option 4 first."; return 0
  fi

  divider
  python3 - "$RESP" <<'PYEOF'
import sys, json
analyses = json.loads(sys.argv[1])[:5]
COLS = {"ALLOW": "\033[32m", "WARN": "\033[33m", "BLOCK": "\033[31m"}
R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
for a in analyses:
    dec   = a.get("decision", "?")
    col   = COLS.get(dec, "")
    risk  = a.get("risk", {})
    score = risk.get("score", "?")
    label = risk.get("label", "")
    cost  = a.get("monthly_cost_delta", "?")
    print(
        f"  {col}{B}[{dec:5}]{R}  "
        f"#{a.get('id', '?'):<4} "
        f"{a.get('project', '?')} / {a.get('environment', '?')}  "
        f"{D}risk={score}/100 {label}  Δcost=${cost}{R}"
    )
PYEOF
  divider
  info "Open dashboard: $API"
}

# ── 6. Verify deployment ──────────────────────────────────────────────────────
verify_deployment() {
  step "6 / Record deployment verification"
  require_server

  LAST_ID="$( [ -f /tmp/.cloudpilot_last_analysis_id ] && cat /tmp/.cloudpilot_last_analysis_id || echo '' )"

  echo
  echo -ne "  ${C_BOLD}Analysis ID to verify${C_RESET} [${C_DIM}${LAST_ID:-none}${C_RESET}]: "
  read -r INPUT_ID
  ANALYSIS_ID="${INPUT_ID:-$LAST_ID}"

  if [ -z "$ANALYSIS_ID" ] || [ "$ANALYSIS_ID" = "?" ]; then
    warn "No analysis ID. Run option 4 first."; return 1
  fi

  echo -ne "  ${C_BOLD}Deployment ID / ticket${C_RESET} [demo-$(date +%Y-%m-%d)]: "
  read -r DEPLOY_ID
  DEPLOY_ID="${DEPLOY_ID:-demo-$(date +%Y-%m-%d)}"

  echo -ne "  ${C_BOLD}Observed monthly cost delta USD${C_RESET} [0]: "
  read -r ACTUAL_COST
  ACTUAL_COST="${ACTUAL_COST:-0}"

  PAYLOAD=$(python3 -c "
import json
print(json.dumps({
  'deployment_identifier':   '$DEPLOY_ID',
  'actual_monthly_cost_delta': float('$ACTUAL_COST'),
  'observed_affected_resources':   ['aws_instance.cloudpilot_demo'],
  'telemetry': {
    'latency_ms_before':  0, 'latency_ms_after':  0,
    'error_rate_before':  0.0, 'error_rate_after':  0.0,
    'availability_before': 1.0, 'availability_after': 1.0
  }
}))")

  info "POST $API/api/analyses/$ANALYSIS_ID/verify …"
  RESP=$(curl -sf -X POST "$API/api/analyses/$ANALYSIS_ID/verify" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>&1) || {
    err "Verification failed."; info "$RESP"; return 1
  }

  ERROR_PCT=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cost_error_percent','?'))" 2>/dev/null || echo "?")
  STATUS=$(echo "$RESP"   | python3 -c "import sys,json; print(json.load(sys.stdin).get('health_status','?'))"         2>/dev/null || echo "?")
  F1_SCORE=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dependency_f1','?'))"         2>/dev/null || echo "?")

  divider
  ok "Verification recorded"
  info "Status      : $STATUS"
  info "Cost error  : $ERROR_PCT%"
  info "Dependency  : $F1_SCORE% F1"
  info "Dashboard   : $API  → Prediction Verification"
  divider
}

# ── 7. Full demo ──────────────────────────────────────────────────────────────
full_demo() {
  echo -e "\n  ${C_GREEN}${C_BOLD}  ▶  Running complete demo  (1 → 5)${C_RESET}"
  echo -e "  ${C_DIM}  AWS check → topology → plan → analyze → view${C_RESET}\n"

  check_aws         || true; echo
  discover_topology || true; echo
  generate_plan     || true; echo
  echo "" | analyze_plan      || true; echo
  view_latest       || true

  echo
  echo -e "  ${C_GREEN}${C_BOLD}  ✓  Demo complete!${C_RESET}"
  echo -e "  ${C_DIM}  Dashboard: $API${C_RESET}"
}

# ── Main loop ─────────────────────────────────────────────────────────────────
main() {
  command -v python3 &>/dev/null || { echo "Error: python3 required." >&2; exit 1; }

  while true; do
    print_header
    print_menu
    read -r CHOICE
    echo

    case "$CHOICE" in
      1) check_aws ;;
      2) discover_topology ;;
      3) generate_plan ;;
      4) analyze_plan ;;
      5) view_latest ;;
      6) verify_deployment ;;
      7) full_demo ;;
      8|q|Q|exit|quit) echo -e "  ${C_DIM}Goodbye!${C_RESET}\n"; exit 0 ;;
      *) warn "Invalid option '$CHOICE'. Choose 1–8." ;;
    esac

    pause
  done
}

main "$@"
