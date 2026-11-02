#!/usr/bin/env bash
# Runs the Pi bug-fixer demo end to end: three real `pi --print` sessions
# against a fresh copy of fixture_repo (the third forked from the second),
# stored in a throwaway session directory, then converted, analyzed and
# checked for drift. No collector: redundo reads Pi's own session files.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDUNDO_ROOT="$(cd "$HERE/../../.." && pwd)"

if ! command -v pi >/dev/null 2>&1; then
  echo "pi isn't on your PATH. Install it first:" >&2
  echo "  npm install -g @earendil-works/pi-coding-agent" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d /tmp/redundo-demo-pi-bug-fixer.XXXXXX)"
SESSIONS_DIR="$WORK_DIR/sessions"
REPO_COPY="$WORK_DIR/fixture_repo"
mkdir -p "$SESSIONS_DIR"
cp -r "$HERE/fixture_repo_template" "$REPO_COPY"

echo "Working directory: $WORK_DIR"
echo "Pi version: $(pi --version)"

# PI_DEMO_MODEL picks the model (any pattern `pi --model` accepts);
# unset, Pi uses whatever default you've configured.
MODEL_ARGS=()
if [[ -n "${PI_DEMO_MODEL:-}" ]]; then
  MODEL_ARGS=(--model "$PI_DEMO_MODEL")
fi

run_pi() {
  (cd "$REPO_COPY" && pi --print --session-dir "$SESSIONS_DIR" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} "$@")
}

echo
echo "Session A: a check that can't pass from inside this repo, repeated..."
run_pi --session-id demo-a \
  "Run exactly this command: python3 -m unittest test_invoice.InvoiceTotalTest -q
If it fails, run exactly the same command once more to rule out a flaky test,
then stop and explain in two sentences what is wrong. Do not modify, create,
move or download any files."

echo
echo "Session B: fix the real bug, then re-run the same tests to confirm..."
run_pi --session-id demo-b \
  "Run exactly this command: python3 -m unittest test_invoice.LineTotalTest -q
Then fix the bug in invoice.py so those tests pass (discount_pct is a
percentage, so 10 means 10%). Only edit invoice.py. Then run exactly the same
command again to confirm, and summarize the fix in one sentence."

B_FILE="$(find "$SESSIONS_DIR" -name '*_demo-b.jsonl' | head -n 1)"
if [[ -z "$B_FILE" ]]; then
  echo "Couldn't find Session B's file under $SESSIONS_DIR" >&2
  exit 1
fi

echo
echo "Session C: a fork of Session B, re-checking and adding a docstring example..."
run_pi --fork "$B_FILE" --session-id demo-c \
  "Run exactly this command again: python3 -m unittest test_invoice.LineTotalTest -q
Then add a one-line usage example to line_total's docstring, and stop."

# --session-dir may hold the files directly or in a per-project
# subdirectory; adapt whichever directory Session B landed in.
ADAPT_DIR="$(dirname "$B_FILE")"

echo
echo "Converting and analyzing the sessions..."
cd "$REDUNDO_ROOT"
uv run redundo adapt "$ADAPT_DIR" --summary -o "$WORK_DIR/trace.jsonl"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format html --output "$WORK_DIR/report.html"

echo
echo "================================================================"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format text
echo "================================================================"
uv run redundo drift "$WORK_DIR/trace.jsonl"
echo "================================================================"
echo
echo "Full report: $WORK_DIR/report.html"
echo "Pi's own session files: $ADAPT_DIR"
