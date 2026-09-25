#!/usr/bin/env bash
# Runs the daily-briefing demo end to end: points openclaw-localtrace at a
# fresh throwaway directory, restarts a local OpenClaw Gateway, drives two
# real conversations through it, converts and analyzes what got captured,
# and prints the report plus where to find the full HTML version.
#
# Requires: `openclaw` on PATH, already configured with a working model
# provider (this uses whatever agent --agent points at, default
# "agentsmith", see `openclaw agents list`), and the openclaw-localtrace
# plugin already installed and enabled (see that plugin's own README).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDUNDO_ROOT="$(cd "$HERE/../../.." && pwd)"
AGENT="${OPENCLAW_DEMO_AGENT:-agentsmith}"
GATEWAY_PORT="${OPENCLAW_DEMO_GATEWAY_PORT:-18789}"

# Send SIGTERM to a PID and wait for it to actually exit, bounded --
# plain `wait "$pid"` blocks indefinitely if the process never dies from
# SIGTERM, and `openclaw gateway run`'s own shutdown path isn't reliably
# fast, or even guaranteed to complete at all, in every environment
# (confirmed: this is what hung the script for one real user, well past
# this "waiting for telemetry to flush" step, with no way out short of a
# manual kill). SIGKILL after the timeout rather than risk blocking
# forever a second time.
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

WORK_DIR="$(mktemp -d /tmp/redundo-demo-openclaw-briefing.XXXXXX)"
TRACES_DIR="$WORK_DIR/traces"
mkdir -p "$TRACES_DIR"
echo "Working directory: $WORK_DIR"

# `cmd || echo null` would NOT do what it looks like here: on failure,
# $(...) still captures whatever `cmd` already wrote to stdout before
# failing (config get's own error is JSON on stdout, not stderr) *and*
# the fallback's output, concatenated, not one or the other. Capture
# and check the exit status separately instead.
if PREVIOUS_OUTPUT_DIR="$(openclaw config get plugins.entries.openclaw-localtrace.config.outputDir --json 2>/dev/null)"; then
  : # PREVIOUS_OUTPUT_DIR holds the real, previously-authored value
else
  PREVIOUS_OUTPUT_DIR="null"
fi

cleanup() {
  # `openclaw gateway run` re-execs into a separate `openclaw-gateway`
  # process whose argv isn't visible to `ps`/`pkill -f` on this platform
  # (it shows up as a bare, argument-less "openclaw-gateway"). Killing
  # just $GATEWAY_PID (the wrapper) can leave the real gateway running
  # and holding the port, orphaned, for the next run to trip over.
  # Killing whatever actually holds the port is precise regardless of
  # process-tree shape or how its argv is reported.
  local port_pid
  port_pid="$(lsof -ti "tcp:$GATEWAY_PORT" 2>/dev/null || true)"
  if [[ -n "$port_pid" ]]; then
    for p in $port_pid; do stop_pid "$p"; done
  fi
  if [[ -n "${GATEWAY_PID:-}" ]]; then
    stop_pid "$GATEWAY_PID"
  fi
  if [[ "$PREVIOUS_OUTPUT_DIR" == "null" ]]; then
    openclaw config unset plugins.entries.openclaw-localtrace.config.outputDir >/dev/null 2>&1 || true
  else
    openclaw config set plugins.entries.openclaw-localtrace.config.outputDir "$(python3 -c "import json,sys; print(json.loads(sys.argv[1]))" "$PREVIOUS_OUTPUT_DIR")" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "Pointing openclaw-localtrace at a throwaway trace directory..."
openclaw config set plugins.entries.openclaw-localtrace.config.outputDir "$TRACES_DIR" >/dev/null

echo "Starting a local OpenClaw Gateway on port $GATEWAY_PORT..."
openclaw gateway run --port "$GATEWAY_PORT" --force > "$WORK_DIR/gateway.log" 2>&1 &
GATEWAY_PID=$!

for _ in $(seq 1 20); do
  if grep -q "http server listening" "$WORK_DIR/gateway.log" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "Running the two conversations against agent \"$AGENT\" (this takes a minute or two)..."
python3 "$HERE/briefing_bot.py" --agent "$AGENT"

echo
echo "Waiting for telemetry to flush..."
sleep 3
stop_pid "$GATEWAY_PID"
unset GATEWAY_PID

echo
echo "Converting and analyzing the capture..."
cd "$REDUNDO_ROOT"
uv run redundo adapt "$TRACES_DIR" --source openclaw-localtrace --summary -o "$WORK_DIR/trace.jsonl"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format html --output "$WORK_DIR/report.html"

echo
echo "================================================================"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format text
echo "================================================================"
echo
echo "Full report: $WORK_DIR/report.html"
