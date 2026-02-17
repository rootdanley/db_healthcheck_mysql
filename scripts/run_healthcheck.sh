#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Uso:
  /opt/db_health/scripts/run_healthcheck.sh --run-yaml /opt/db_health/instances/<inst>/run.yaml [--week YYYY-MM-DD] [--stmt-chars N]

Exemplos:
  /opt/db_health/scripts/run_healthcheck.sh --run-yaml /opt/db_health/instances/crmbonus/run.yaml --week 2026-02-15
  /opt/db_health/scripts/run_healthcheck.sh --run-yaml /opt/db_health/instances/crmbonus/run.yaml --stmt-chars 1600
EOF
}

die() { echo "[ERRO] $*" >&2; exit 1; }
warn() { echo "[WARN] $*" >&2; }

RUN_YAML=""
WEEK=""
STMT_CHARS="800"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-yaml) RUN_YAML="${2:-}"; shift 2 ;;
    --week) WEEK="${2:-}"; shift 2 ;;
    --stmt-chars) STMT_CHARS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Argumento desconhecido: $1 (use --help)" ;;
  esac
done

[[ -n "$RUN_YAML" ]] || die "Faltou --run-yaml"
[[ -f "$RUN_YAML" ]] || die "run.yaml não encontrado: $RUN_YAML"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"              # /opt/db_health
INSTANCE_DIR="$(cd "$(dirname "$RUN_YAML")" && pwd)"    # /opt/db_health/instances/<inst>
INSTANCE="$(basename "$INSTANCE_DIR")"

PY="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3 || true)"
fi
[[ -n "$PY" ]] || die "python3 não encontrado e ${ROOT_DIR}/.venv/bin/python não existe"

supports_arg() {
  local script="$1"
  local flag="$2"
  "$PY" "$script" --help 2>&1 | grep -q -- "$flag"
}

yaml_get() {
  # uso: yaml_get <run_yaml> <expr_python>
  # expr_python pode usar cfg dict e deve imprimir string.
  local run_yaml="$1"
  local expr="$2"
  "$PY" - "$run_yaml" "$expr" <<'PY'
import sys
from pathlib import Path
import yaml
p = Path(sys.argv[1])
expr = sys.argv[2]
cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
try:
    out = eval(expr, {"cfg": cfg})
except Exception:
    out = ""
print(str(out or "").strip())
PY
}

# WEEK do YAML se não vier por CLI
if [[ -z "$WEEK" ]]; then
  WEEK="$(yaml_get "$RUN_YAML" 'cfg.get("week","")')"
fi
[[ -n "$WEEK" ]] || die "Week não definida. Passe --week YYYY-MM-DD ou defina week no run.yaml."

# Shared support dir (default: /opt/db_health/shared/support)
SHARED_SUPPORT_DIR="$(yaml_get "$RUN_YAML" '(cfg.get("shared") or {}).get("support_dir","")')"
if [[ -z "$SHARED_SUPPORT_DIR" ]]; then
  SHARED_SUPPORT_DIR="${ROOT_DIR}/shared/support"
fi
[[ -d "$SHARED_SUPPORT_DIR" ]] || die "shared support dir não encontrado: $SHARED_SUPPORT_DIR"

# monta WEEK_ARGS sem gerar argumento vazio
week_args_for() {
  WEEK_ARGS=()
  local script="$1"
  if supports_arg "$script" "--week"; then
    WEEK_ARGS+=(--week "$WEEK")
  fi
}

run_step() {
  local name="$1"; shift
  echo ""
  echo "============================================================"
  echo "[STEP] ${name}"
  printf "[CMD ]"; printf " %q" "$@"; echo
  echo "------------------------------------------------------------"
  "$@"
}

# Paths por week (compatível com teu tree)
L0_IN="${INSTANCE_DIR}/layer0/inputs/${WEEK}"
L0_COL="${INSTANCE_DIR}/layer0/collected/${WEEK}"

LA_IN="${INSTANCE_DIR}/layerA/inputs/${WEEK}"
LA_COL="${INSTANCE_DIR}/layerA/collected/${WEEK}"

LB_IN="${INSTANCE_DIR}/layerB/inputs/${WEEK}"
LB_COL="${INSTANCE_DIR}/layerB/collected/${WEEK}"

LC_IN="${INSTANCE_DIR}/layerC/inputs/${WEEK}"
LC_COL="${INSTANCE_DIR}/layerC/collected/${WEEK}"

R_HTML_DIR="${INSTANCE_DIR}/reports/html/${WEEK}"
R_FIG_DIR="${INSTANCE_DIR}/reports/figures/${WEEK}"

LOG_DIR="${INSTANCE_DIR}/logs/${WEEK}"
TMP_DIR="${INSTANCE_DIR}/tmp/${WEEK}"

mkdir -p \
  "$L0_IN" "$L0_COL" \
  "$LA_IN" "$LA_COL" \
  "$LB_IN" "$LB_COL" \
  "$LC_IN" "$LC_COL" \
  "$R_HTML_DIR" "$R_FIG_DIR" \
  "$LOG_DIR" "$TMP_DIR"

LOG_FILE="${LOG_DIR}/run_healthcheck.log"

# log geral (stdout+stderr)
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[INFO] instance=${INSTANCE} week=${WEEK}"
echo "[INFO] root=${ROOT_DIR}"
echo "[INFO] shared_support_dir=${SHARED_SUPPORT_DIR}"
echo "[INFO] logs=${LOG_FILE}"

# Scripts
S_L0_NORM="${ROOT_DIR}/scripts/layer0/normalize_layer0_inventory.py"
S_LA_NORM="${ROOT_DIR}/scripts/layerA/normalize_cloudwatch.py"
S_LB_NORM="${ROOT_DIR}/scripts/layerB/normalize_layerB_collected.py"
S_LC_NORM="${ROOT_DIR}/scripts/layerC/normalize_layerC_cloudwatch_slowlog.py"

S_L0_REP="${ROOT_DIR}/scripts/report/report_layer0_inventory.py"
S_LA_REP="${ROOT_DIR}/scripts/report/report_layerA_cloudwatch.py"
S_LB_REP="${ROOT_DIR}/scripts/report/report_layerB_objects.py"
S_LC_REP="${ROOT_DIR}/scripts/report/report_layerC_cloudwatch_slowlog.py"
S_FULL_REP="${ROOT_DIR}/scripts/report/report_full_healthcheck.py"

# ----------------------------
# Normalize
# ----------------------------

if [[ -f "${L0_IN}/layer0_inventory.csv" ]]; then
  [[ -f "$S_L0_NORM" ]] || warn "Layer0 normalize não encontrado: $S_L0_NORM"
  if [[ -f "$S_L0_NORM" ]]; then
    week_args_for "$S_L0_NORM"
    L0_EXTRA=()
    if supports_arg "$S_L0_NORM" "--shared-support-dir"; then
      L0_EXTRA+=(--shared-support-dir "$SHARED_SUPPORT_DIR")
    fi
    run_step "Layer0 normalize" "$PY" "$S_L0_NORM" --run-yaml "$RUN_YAML" "${L0_EXTRA[@]}" "${WEEK_ARGS[@]}"
  fi
else
  warn "Layer0: sem layer0_inventory.csv em ${L0_IN} (pulando normalize)"
fi

if compgen -G "${LA_IN}/*.csv" > /dev/null; then
  [[ -f "$S_LA_NORM" ]] || warn "LayerA normalize não encontrado: $S_LA_NORM"
  if [[ -f "$S_LA_NORM" ]]; then
    week_args_for "$S_LA_NORM"
    LA_EXTRA=()
    supports_arg "$S_LA_NORM" "--input-dir"  && LA_EXTRA+=(--input-dir "$LA_IN")
    supports_arg "$S_LA_NORM" "--output-dir" && LA_EXTRA+=(--output-dir "$LA_COL")
    run_step "LayerA normalize" "$PY" "$S_LA_NORM" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}" "${LA_EXTRA[@]}"
  fi
else
  warn "LayerA: sem CSVs em ${LA_IN} (pulando normalize)"
fi

if compgen -G "${LB_IN}/*.tsv" > /dev/null; then
  [[ -f "$S_LB_NORM" ]] || warn "LayerB normalize não encontrado: $S_LB_NORM"
  if [[ -f "$S_LB_NORM" ]]; then
    week_args_for "$S_LB_NORM"
    LB_EXTRA=()
    supports_arg "$S_LB_NORM" "--in-dir"  && LB_EXTRA+=(--in-dir "$LB_IN")
    supports_arg "$S_LB_NORM" "--out-dir" && LB_EXTRA+=(--out-dir "$LB_COL")
    run_step "LayerB normalize" "$PY" "$S_LB_NORM" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}" "${LB_EXTRA[@]}"
  fi
else
  warn "LayerB: sem TSVs em ${LB_IN} (pulando normalize)"
fi

if compgen -G "${LC_IN}/*.csv" > /dev/null; then
  [[ -f "$S_LC_NORM" ]] || warn "LayerC normalize não encontrado: $S_LC_NORM"
  if [[ -f "$S_LC_NORM" ]]; then
    week_args_for "$S_LC_NORM"
    LC_EXTRA=()
    supports_arg "$S_LC_NORM" "--in-dir"  && LC_EXTRA+=(--in-dir "$LC_IN")
    supports_arg "$S_LC_NORM" "--out-dir" && LC_EXTRA+=(--out-dir "$LC_COL")
    run_step "LayerC normalize" "$PY" "$S_LC_NORM" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}" "${LC_EXTRA[@]}"
  fi
else
  warn "LayerC: sem CSVs em ${LC_IN} (pulando normalize)"
fi

# ----------------------------
# Reports
# ----------------------------
if [[ -f "$S_L0_REP" ]]; then
  week_args_for "$S_L0_REP"
  run_step "Report Layer0" "$PY" "$S_L0_REP" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}"
else
  warn "Report Layer0 não encontrado: $S_L0_REP"
fi

if [[ -f "$S_LA_REP" ]]; then
  week_args_for "$S_LA_REP"
  run_step "Report LayerA" "$PY" "$S_LA_REP" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}"
else
  warn "Report LayerA não encontrado: $S_LA_REP"
fi

if [[ -f "$S_LB_REP" ]]; then
  week_args_for "$S_LB_REP"
  run_step "Report LayerB" "$PY" "$S_LB_REP" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}"
else
  warn "Report LayerB não encontrado: $S_LB_REP"
fi

if [[ -f "$S_LC_REP" ]]; then
  week_args_for "$S_LC_REP"
  LC_REP_EXTRA=()
  supports_arg "$S_LC_REP" "--stmt-chars" && LC_REP_EXTRA+=(--stmt-chars "$STMT_CHARS")
  run_step "Report LayerC" "$PY" "$S_LC_REP" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}" "${LC_REP_EXTRA[@]}"
else
  warn "Report LayerC não encontrado: $S_LC_REP"
fi

if [[ -f "$S_FULL_REP" ]]; then
  week_args_for "$S_FULL_REP"
  run_step "Report FULL" "$PY" "$S_FULL_REP" --run-yaml "$RUN_YAML" "${WEEK_ARGS[@]}"
else
  warn "Report FULL não encontrado: $S_FULL_REP"
fi

echo ""
echo "[OK] Finalizado."
echo "[OK] HTMLs: ${R_HTML_DIR}"
echo "[OK] Log:   ${LOG_FILE}"
