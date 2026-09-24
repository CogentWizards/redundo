#!/usr/bin/env bash
# Runs the code-review-bot demo end to end: starts a local OTLP receiver,
# drives two real Claude Agent SDK sessions against a fresh copy of
# fixture_repo, converts and analyzes what got captured, and prints the
# report plus a short note on what its confirmed_waste count means.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "$HERE/venv" ]]; then
  echo "First run: setting up a local venv with the Claude Agent SDK..."
  python3 -m venv "$HERE/venv"
  "$HERE/venv/bin/pip" install -q --upgrade pip claude-agent-sdk
fi

WORK_DIR="$(mktemp -d /tmp/redundo-demo-code-review.XXXXXX)"
TRACES_DIR="$WORK_DIR/traces"
REPO_COPY="$WORK_DIR/fixture_repo"
mkdir -p "$TRACES_DIR"
cp -r "$HERE/fixture_repo_template" "$REPO_COPY"

echo "Working directory: $WORK_DIR"

echo "Pinning a test environment inside the fixture repo (pytest)..."
python3 -m venv "$REPO_COPY/.venv"
"$REPO_COPY/.venv/bin/pip" install -q pytest

REDUNDO_ROOT="$(cd "$HERE/../../.." && pwd)"

cleanup() {
  # `uv run` doesn't reliably forward SIGTERM to the actual `redundo
  # collect` child process it spawns. Killing just $COLLECT_PID (the
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

echo "Running the two Claude Agent SDK sessions (this takes a couple of minutes)..."
"$HERE/venv/bin/python3" "$HERE/review_bot.py" --repo "$REPO_COPY"

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
uv run redundo adapt "$TRACES_DIR" --source claude-code --summary -o "$WORK_DIR/trace.jsonl"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format html --output "$WORK_DIR/report.html"

echo
echo "================================================================"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format text
echo "================================================================"
echo
echo "If confirmed_waste reads 0 above: that's not this tool missing"
echo "Session A's repeat, it sees it. Claude Code's own telemetry has no"
echo "way to say the TASK failed, only that the API call succeeded, and"
echo "redundo refuses to guess past that. See the 'unclassified' reason"
echo "string above, and this app's own README, for the full story."
echo
echo "Full report: $WORK_DIR/report.html"
