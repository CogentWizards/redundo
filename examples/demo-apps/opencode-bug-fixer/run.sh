#!/usr/bin/env bash
# Runs the OpenCode bug-fixer demo end to end: three real `opencode run`
# sessions against a fresh git copy of fixture_repo (the third delegating
# to a subagent), exports exactly those sessions, then converts and
# analyzes them. No collector: redundo reads OpenCode's own session data.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDUNDO_ROOT="$(cd "$HERE/../../.." && pwd)"

if ! command -v opencode >/dev/null 2>&1; then
  echo "opencode isn't on your PATH. Install it first:" >&2
  echo "  npm install -g opencode-ai" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d /tmp/redundo-demo-opencode-bug-fixer.XXXXXX)"
EXPORTS_DIR="$WORK_DIR/exports"
REPO_COPY="$WORK_DIR/fixture_repo"
mkdir -p "$EXPORTS_DIR"
cp -r "$HERE/fixture_repo_template" "$REPO_COPY"
# A git repo, so OpenCode's per-step snapshots record which steps changed files.
git -C "$REPO_COPY" init -q
git -C "$REPO_COPY" add -A
git -C "$REPO_COPY" -c user.name=demo -c user.email=demo@example.invalid commit -q -m fixture

# Keep Python from writing __pycache__ into the copy. OpenCode's snapshot
# would otherwise record every test run as a step that changed files.
export PYTHONDONTWRITEBYTECODE=1

echo "Working directory: $WORK_DIR"
echo "OpenCode version: $(opencode --version)"

# OPENCODE_DEMO_MODEL picks the model (provider/model); unset, OpenCode
# uses your configured default.
MODEL_ARGS=()
if [[ -n "${OPENCODE_DEMO_MODEL:-}" ]]; then
  MODEL_ARGS=(--model "$OPENCODE_DEMO_MODEL")
fi

# --auto approves tool permissions without asking. Safe here: everything
# runs inside the throwaway copy above.
run_opencode() {
  opencode run --dir "$REPO_COPY" --auto ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} "$@"
}

echo
echo "Session A: a check that can't pass from inside this repo, repeated..."
run_opencode --title "redundo demo A" \
  "Run exactly this command: python3 -m unittest test_invoice.InvoiceTotalTest -q
If it fails, run exactly the same command once more to rule out a flaky test,
then stop and explain in two sentences what is wrong. Do not modify, create,
move or download any files."

echo
echo "Session B: fix the real bug, then re-run the same tests to confirm..."
run_opencode --title "redundo demo B" \
  "Run exactly this command: python3 -m unittest test_invoice.LineTotalTest -q
Then fix the bug in invoice.py so those tests pass (discount_pct is a
percentage, so 10 means 10%). Only edit invoice.py. Then run exactly the same
command again to confirm, and summarize the fix in one sentence."

echo
echo "Session C: delegate a check to a subagent, then double-check it..."
run_opencode --title "redundo demo C" \
  "Use the task tool to have the general subagent run exactly this command and
report the result: python3 -m unittest test_invoice.LineTotalTest -q
When it reports back, run exactly the same command yourself to double-check,
then stop. Do not modify any files."

echo
echo "Exporting just this demo's sessions (subagent sessions included)..."
# Match on the throwaway directory, so nothing else in your OpenCode
# history is touched. Paths are stored as-is; resolve /tmp symlinks first.
REPO_REAL="$(cd "$REPO_COPY" && pwd -P)"
SESSION_IDS="$(opencode db --format json \
  "SELECT id FROM session WHERE directory IN ('$REPO_COPY', '$REPO_REAL') ORDER BY time_created" \
  | python3 -c 'import json, sys; print("\n".join(row["id"] for row in json.load(sys.stdin)))')"
if [[ -z "$SESSION_IDS" ]]; then
  echo "Found no sessions for $REPO_COPY in OpenCode's database" >&2
  exit 1
fi
while read -r id; do
  opencode export "$id" > "$EXPORTS_DIR/$id.json" 2>/dev/null
  echo "  $id"
done <<< "$SESSION_IDS"

echo
echo "Converting and analyzing the sessions..."
cd "$REDUNDO_ROOT"
uv run redundo adapt "$EXPORTS_DIR" --summary -o "$WORK_DIR/trace.jsonl"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format html --output "$WORK_DIR/report.html"

echo
echo "================================================================"
uv run redundo analyze "$WORK_DIR/trace.jsonl" --format text
echo "================================================================"
echo
echo "Full report: $WORK_DIR/report.html"
echo "Exported sessions: $EXPORTS_DIR"
