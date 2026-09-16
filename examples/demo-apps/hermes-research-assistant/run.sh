#!/usr/bin/env bash
# Runs the Hermes research-assistant demo end to end: starts a local OTLP
# receiver (hermes-otel's own config, ~/.hermes/hermes_otel.yaml, points at
# a fixed http://localhost:4318/v1/traces -- no per-run override, so this
# script owns that port for the duration of the run), drives two real
# `hermes` conversations, converts and analyzes what got captured, and
# prints the report plus where to find the full HTML version.
#
# Requires: `hermes` on PATH, already logged in with a working model
# provider, and hermes-otel already configured to export to
# http://localhost:4318/v1/traces (see ~/.hermes/hermes_otel.yaml).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDUNDO_ROOT="$(cd "$HERE/../../.." && pwd)"

WORK_DIR="$(mktemp -d /tmp/redundo-demo-hermes-research.XXXXXX)"
TRACES_DIR="$WORK_DIR/traces"
mkdir -p "$TRACES_DIR"
echo "Working directory: $WORK_DIR"

cleanup() {
  # `uv run` doesn't reliably forward SIGTERM to the actual `redundo
  # collect` child process it spawns -- killing just $COLLECT_PID (the
  # `uv run` wrapper) can leave the real collector running and holding
  # the port, orphaned, for every later run to trip over. Match on the
  # trace dir, which is unique per run, to find and kill the real
  # process regardless of the process tree shape.
  pkill -f "redundo collect --out-dir $TRACES_DIR" 2>/dev/null || true
  if [[ -n "${COLLECT_PID:-}" ]]; then
    kill "$COLLECT_PID" 2>/dev/null || true
    wait "$COLLECT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "Starting a local OTLP receiver..."
(cd "$REDUNDO_ROOT" && uv run --extra collector redundo collect --out-dir "$TRACES_DIR") \
  > "$WORK_DIR/collect.log" 2>&1 &
COLLECT_PID=$!
sleep 2

echo "Running the two Hermes conversations (this takes a couple of minutes)..."
python3 "$HERE/research_bot.py"

echo
echo "Waiting for telemetry to flush..."
sleep 3
pkill -f "redundo collect --out-dir $TRACES_DIR" 2>/dev/null || true
kill "$COLLECT_PID" 2>/dev/null || true
wait "$COLLECT_PID" 2>/dev/null || true
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
