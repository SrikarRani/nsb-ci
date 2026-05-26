#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DAEMON_DIR="${DAEMON_DIR:-${REPO_ROOT}}"
SIM_WORKDIR="${SIM_WORKDIR:-${REPO_ROOT}}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
DAEMON_CMD="${DAEMON_CMD:-./build/nsb_daemon config-ghost-perf.yaml}"
SIM_CMD="${SIM_CMD:-}"
PYTHON_CMD_BASE="${PYTHON_CMD_BASE:-}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-1}"
STARTUP_SLEEP="${STARTUP_SLEEP:-3}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${REPO_ROOT}/results}"
DAEMON_PORT="${DAEMON_PORT:-65432}"

RUN_TAG=""
PY_ARGS=()
NODES_ARG_VALUE=""

usage() {
  cat <<'EOF'
Usage:
  run_ghost_perf_capture.sh [options] -- [python_test_args]

Options:
  --daemon-dir PATH        Path to the NSB repo (default: repo root)
  --sim-workdir PATH       Working directory for the ghost simulator command
  --venv-dir PATH          Python virtualenv dir (default: <repo>/.venv)
  --daemon-cmd "CMD"       Daemon command run inside --daemon-dir
  --sim-cmd "CMD"          Ghost simulator command run inside --sim-workdir
  --python-cmd "CMD"       Python test base command run inside --daemon-dir
  --sample-interval SEC    CPU/memory sampling interval (default: 1)
  --startup-sleep SEC      Sleep after daemon and simulator startup (default: 3)
  --output-root PATH       Root directory for artifacts
  --tag NAME               Optional run tag suffix in output directory name
  -h, --help               Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --daemon-dir) DAEMON_DIR="$2"; shift 2 ;;
    --sim-workdir) SIM_WORKDIR="$2"; shift 2 ;;
    --venv-dir) VENV_DIR="$2"; shift 2 ;;
    --daemon-cmd) DAEMON_CMD="$2"; shift 2 ;;
    --sim-cmd) SIM_CMD="$2"; shift 2 ;;
    --python-cmd) PYTHON_CMD_BASE="$2"; shift 2 ;;
    --sample-interval) SAMPLE_INTERVAL="$2"; shift 2 ;;
    --startup-sleep) STARTUP_SLEEP="$2"; shift 2 ;;
    --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
    --tag) RUN_TAG="$2"; shift 2 ;;
    --) shift; PY_ARGS=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

for ((i=0; i<${#PY_ARGS[@]}; i++)); do
  arg="${PY_ARGS[$i]}"
  if [[ "$arg" == "--nodes" ]]; then
    if (( i + 1 < ${#PY_ARGS[@]} )); then
      NODES_ARG_VALUE="${PY_ARGS[$((i+1))]}"
    fi
    break
  elif [[ "$arg" == --nodes=* ]]; then
    NODES_ARG_VALUE="${arg#--nodes=}"
    break
  fi
done

if [[ -z "${PYTHON_CMD_BASE:-}" ]]; then
  PYTHON_CMD_BASE="${VENV_DIR}/bin/python ${REPO_ROOT}/.github/scripts/ghost_perf_test_client.py"
fi

REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-${REPO_ROOT}/python/requirements.txt}"
if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "Missing requirements file: $REQUIREMENTS_FILE" >&2
  exit 1
fi

setup_python_venv() {
  echo "Setting up python venv at: $VENV_DIR"
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Venv python not found at: $VENV_DIR/bin/python" >&2
    exit 1
  fi
  "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"
}

setup_python_venv

if [[ -z "${SIM_CMD:-}" ]]; then
  SIM_CMD="${VENV_DIR}/bin/python ${REPO_ROOT}/.github/scripts/ghost_simulator.py --identifier ghost"
fi

if [[ -n "${NODES_ARG_VALUE:-}" ]]; then
  SIM_SOURCES=""
  for ((i=0; i<${NODES_ARG_VALUE}; i++)); do
    source="host${i}"
    if [[ -z "$SIM_SOURCES" ]]; then
      SIM_SOURCES="$source"
    else
      SIM_SOURCES="${SIM_SOURCES},${source}"
    fi
  done
  SIM_CMD="${SIM_CMD} --sources ${SIM_SOURCES}"
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
if [[ -n "$RUN_TAG" ]]; then
  RUN_DIR="$OUTPUT_ROOT/${TIMESTAMP}_${RUN_TAG}"
else
  RUN_DIR="$OUTPUT_ROOT/$TIMESTAMP"
fi
mkdir -p "$RUN_DIR"

DAEMON_LOG="$RUN_DIR/daemon.log"
SIM_LOG="$RUN_DIR/ghost_simulator.log"
PYTHON_LOG="$RUN_DIR/python_test.log"
SAMPLES_CSV="$RUN_DIR/daemon_resource_samples.csv"
RESOURCE_STATS="$RUN_DIR/daemon_resource_stats.txt"
RUN_CONFIG="$RUN_DIR/run_config.txt"
PARSED_METRICS="$RUN_DIR/parsed_test_metrics.txt"

DAEMON_PID=""
SIM_PID=""
MONITOR_PID=""

cleanup() {
  set +e

  if [[ -n "$MONITOR_PID" ]] && kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill -TERM "$MONITOR_PID" 2>/dev/null
    wait "$MONITOR_PID" 2>/dev/null
  fi

  if [[ -n "$SIM_PID" ]] && kill -0 "$SIM_PID" 2>/dev/null; then
    kill -INT "$SIM_PID" 2>/dev/null
    sleep 1
    kill -TERM "$SIM_PID" 2>/dev/null
    sleep 1
    kill -KILL "$SIM_PID" 2>/dev/null
    wait "$SIM_PID" 2>/dev/null || true
  fi

  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -INT "$DAEMON_PID" 2>/dev/null
    sleep 1
    kill -TERM "$DAEMON_PID" 2>/dev/null
    sleep 1
    kill -KILL "$DAEMON_PID" 2>/dev/null
    wait "$DAEMON_PID" 2>/dev/null || true
  fi

  for _ in $(seq 1 20); do
    if ! ss -ltn 2>/dev/null | awk -v port=":${DAEMON_PORT}" '$4 ~ (port "$") {found=1} END {exit found ? 0 : 1}'; then
      break
    fi
    sleep 1
  done
}

trap cleanup EXIT

capture_git_state() {
  local dir="$1"
  local label="$2"
  {
    echo "[$label] dir: $dir"
    if command -v git >/dev/null 2>&1 && git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[$label] git_commit: $(git -C "$dir" rev-parse HEAD)"
      echo "[$label] git_branch: $(git -C "$dir" rev-parse --abbrev-ref HEAD)"
      echo "[$label] git_dirty: $(git -C "$dir" status --porcelain | wc -l | tr -d ' ') files changed"
    else
      echo "[$label] git: unavailable"
    fi
  } >> "$RUN_CONFIG"
}

{
  echo "run_timestamp: $TIMESTAMP"
  echo "run_dir: $RUN_DIR"
  echo "daemon_dir: $DAEMON_DIR"
  echo "sim_workdir: $SIM_WORKDIR"
  echo "venv_dir: $VENV_DIR"
  echo "requirements_file: $REQUIREMENTS_FILE"
  echo "daemon_cmd: $DAEMON_CMD"
  echo "sim_cmd: $SIM_CMD"
  echo "python_cmd_base: $PYTHON_CMD_BASE"
  echo "python_args: ${PY_ARGS[*]:-<none>}"
  echo "nodes_arg_value: ${NODES_ARG_VALUE:-<unset>}"
  echo "sample_interval_s: $SAMPLE_INTERVAL"
  echo "startup_sleep_s: $STARTUP_SLEEP"
  echo "host: $(hostname)"
  echo "uname: $(uname -a)"
} > "$RUN_CONFIG"

capture_git_state "$DAEMON_DIR" "daemon_repo"

if [[ -f "$DAEMON_DIR/config-ghost-perf.yaml" ]]; then
  cp "$DAEMON_DIR/config-ghost-perf.yaml" "$RUN_DIR/config.yaml"
fi

printf "timestamp_utc,epoch_s,cpu_percent,rss_kb,rss_mb\n" > "$SAMPLES_CSV"

monitor_daemon_resources() {
  local pid="$1"
  while kill -0 "$pid" 2>/dev/null; do
    local ts epoch ps_out cpu rss rss_mb
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    epoch="$(date +%s)"
    ps_out="$(ps -p "$pid" -o %cpu= -o rss= 2>/dev/null | awk '{print $1, $2}')"
    if [[ -n "$ps_out" ]]; then
      cpu="$(awk '{print $1}' <<< "$ps_out")"
      rss="$(awk '{print $2}' <<< "$ps_out")"
      rss_mb="$(awk -v kb="$rss" 'BEGIN{printf "%.3f", kb/1024.0}')"
      printf "%s,%s,%s,%s,%s\n" "$ts" "$epoch" "$cpu" "$rss" "$rss_mb" >> "$SAMPLES_CSV"
    fi
    sleep "$SAMPLE_INTERVAL"
  done
}

run_cmd_in_dir_bg() {
  local dir="$1"
  local cmd="$2"
  local log="$3"
  (
    cd "$dir"
    bash -lc "exec $cmd"
  ) > "$log" 2>&1 &
  echo $!
}

echo "[1/3] Starting NSB daemon..."
DAEMON_PID="$(run_cmd_in_dir_bg "$DAEMON_DIR" "$DAEMON_CMD" "$DAEMON_LOG")"
sleep "$STARTUP_SLEEP"
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
  echo "Daemon failed to start. See $DAEMON_LOG" >&2
  exit 1
fi

echo "[2/3] Starting ghost simulator..."
SIM_PID="$(run_cmd_in_dir_bg "$SIM_WORKDIR" "$SIM_CMD" "$SIM_LOG")"
sleep "$STARTUP_SLEEP"
if ! kill -0 "$SIM_PID" 2>/dev/null; then
  echo "Ghost simulator failed to start. See $SIM_LOG" >&2
  exit 1
fi

echo "Starting daemon resource monitor..."
monitor_daemon_resources "$DAEMON_PID" &
MONITOR_PID=$!

echo "[3/3] Running python performance test..."
PY_CMD_FULL="$PYTHON_CMD_BASE"
if [[ ${#PY_ARGS[@]} -gt 0 ]]; then
  for arg in "${PY_ARGS[@]}"; do
    PY_CMD_FULL+=" $(printf '%q' "$arg")"
  done
fi

set +e
(
  cd "$DAEMON_DIR"
  NSB_PYTHON_DIR="$REPO_ROOT/python" \
  PYTHONPATH="$REPO_ROOT/python:${PYTHONPATH:-}" \
  bash -lc "$PY_CMD_FULL"
) | tee "$PYTHON_LOG"
PY_EXIT=${PIPESTATUS[0]}
set -e

if [[ -n "$MONITOR_PID" ]] && kill -0 "$MONITOR_PID" 2>/dev/null; then
  kill -TERM "$MONITOR_PID" 2>/dev/null
  wait "$MONITOR_PID" 2>/dev/null || true
fi

awk -F',' '
  NR==2 {cpu_min=$3; cpu_max=$3; cpu_sum=$3; mem_min=$5; mem_max=$5; mem_sum=$5; n=1; next}
  NR>2  {if($3<cpu_min)cpu_min=$3; if($3>cpu_max)cpu_max=$3; cpu_sum+=$3;
         if($5<mem_min)mem_min=$5; if($5>mem_max)mem_max=$5; mem_sum+=$5; n++}
  END {
    if (n>0) {
      printf "samples=%d\n", n;
      printf "cpu_percent_min=%.3f\n", cpu_min;
      printf "cpu_percent_avg=%.3f\n", cpu_sum/n;
      printf "cpu_percent_max=%.3f\n", cpu_max;
      printf "rss_mb_min=%.3f\n", mem_min;
      printf "rss_mb_avg=%.3f\n", mem_sum/n;
      printf "rss_mb_max=%.3f\n", mem_max;
    } else {
      print "samples=0";
      print "cpu_percent_min=NA";
      print "cpu_percent_avg=NA";
      print "cpu_percent_max=NA";
      print "rss_mb_min=NA";
      print "rss_mb_avg=NA";
      print "rss_mb_max=NA";
    }
  }
' "$SAMPLES_CSV" > "$RESOURCE_STATS"

awk '/^==== SUMMARY ====$/,/^=+$/' "$PYTHON_LOG" > "$PARSED_METRICS"

{
  echo "python_exit_code: $PY_EXIT"
  echo "daemon_pid: $DAEMON_PID"
  echo "sim_pid: $SIM_PID"
} >> "$RUN_CONFIG"

echo "Run artifacts saved to: $RUN_DIR"
echo "- Config:         $RUN_CONFIG"
echo "- Daemon log:     $DAEMON_LOG"
echo "- Ghost sim log:  $SIM_LOG"
echo "- Python log:     $PYTHON_LOG"
echo "- Samples CSV:    $SAMPLES_CSV"
echo "- Resource stats: $RESOURCE_STATS"
echo "- Parsed metrics: $PARSED_METRICS"

exit "$PY_EXIT"
