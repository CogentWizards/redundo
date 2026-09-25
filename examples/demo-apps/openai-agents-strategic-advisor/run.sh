#!/usr/bin/env bash
# Runs the strategic-advisor demo end to end: starts a local OTLP
# receiver, drives two real OpenAI Agents SDK sessions (one with a real
# handoff, one single-agent), converts and analyzes what got captured,
# and prints the report plus where to find the full HTML version.
#
# Requires: OPENAI_API_KEY set in your environment.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDUNDO_ROOT="$(cd "$HERE/../../.." && pwd)"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not set. Export it and re-run." >&2
  exit 1
fi

if [[ ! -d "$HERE/venv" ]]; then
  echo "First run: setting up a local venv with the OpenAI Agents SDK..."
  python3 -m venv "$HERE/venv"
  "$HERE/venv/bin/pip" install -q --upgrade pip \
    openai-agents openinference-instrumentation-openai-agents \
    opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
fi

WORK_DIR="$(mktemp -d /tmp/redundo-demo-openai-agents-advisor.XXXXXX)"
TRACES_DIR="$WORK_DIR/traces"
mkdir -p "$TRACES_DIR"
echo "Working directory: $WORK_DIR"

# Send SIGTERM to a PID and wait for it to actually exit, bounded --
# plain `wait "$pid"` blocks indefinitely if the process never dies from
# SIGTERM, and there's no guarantee it always will in every environment.
# SIGKILL after the timeout rather than risk blocking forever.
stop_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill "$pid" 2>/dev/null || return 0
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
}

cleanup() {
  # `uv run` doesn't reliably forward SIGTERM to the actual `redundo
  # collect` child process it spawns. Killing just $COLLECT_PID (the
  # `uv run` wrapper) can leave the real collector running and holding
  # the port, orphaned, for every later run to trip over. Match on the
  # trace dir, which is unique per run, to find and kill the real
  # process regardless of the process tree shape.
  pkill -f "redundo collect --out-dir $TRACES_DIR" 2>/dev/null || true
  if [[ -n "${COLLECT_PID:-}" ]]; then
    stop_pid "$COLLECT_PID"
  fi
}
trap cleanup EXIT

echo "Starting a local OTLP receiver..."
(cd "$REDUNDO_ROOT" && uv run --extra collector redundo collect --out-dir "$TRACES_DIR") \
  > "$WORK_DIR/collect.log" 2>&1 &
COLLECT_PID=$!
sleep 2

echo "Running the two OpenAI Agents SDK sessions (this takes a minute or two)..."
"$HERE/venv/bin/python3" "$HERE/advisor_bot.py"

echo
echo "Waiting for telemetry to flush..."
sleep 3
pkill -f "redundo collect --out-dir $TRACES_DIR" 2>/dev/null || true
stop_pid "$COLLECT_PID"
unset COLLECT_PID

echo
echo "Converting and analyzing the capture..."
cd "$REDUNDO_ROOT"
uv run redundo adapt "$TRACES_DIR" --source openinference --summary -o "$WORK_DIR/trace.jsonl"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format html --output "$WORK_DIR/report.html"

echo
echo "================================================================"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format text
echo "================================================================"
echo
echo "Full report: $WORK_DIR/report.html"
