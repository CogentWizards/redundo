"""A tiny "daily briefing assistant" built on OpenClaw and our own
openclaw-localtrace plugin -- the redundo demo app for the
openclaw-localtrace adapter. Drives two real conversations against a
real, running OpenClaw Gateway (one `openclaw agent` CLI invocation per
turn, --session-key keeping each conversation's turns in one session),
and prints each turn's reply as it comes back.

Session A asks for a quick tech-news search, gets a generic,
unhelpful result, and re-runs the *exact same* search once more before
giving up -- a repeated, identical tool call with nothing written in
between. On real data this lands in `unclassified`, not
`confirmed_waste`, for the same reason Session A does in the
claude-agent-sdk-code-review demo: the task's last recorded event is the
model's own closing reply, which succeeds as a model call regardless of
what it says, so the trace's own terminal-outcome signal reads "success"
even though a human would call the repeat pointless. See that demo's
README for the fuller explanation; it's the same limitation, confirmed a
second time against a different real source.

Session B searches, saves the result to a file (a real write --
`write_file` is one of openclaw-localtrace's default `mutatingToolNames`),
then re-runs the *exact same* search again to double check before
sending -- a real write-then-recheck loop, `likely_legitimate` regardless
of whether the search results themselves happened to change. Two more
searches, each rephrased to look at the story from a different angle,
give two `near_duplicate` candidates.

The exact tool arguments (query/count/freshness) are spelled out
explicitly in the prompts and asked for "verbatim" on repeats, since
redundo's exact-match detection compares the whole tool call, and
leaving the model to reword a query on its own -- even one meant to be
identical -- would land the repeat in near_duplicate instead of the
clean exact-match story these sessions are built to demonstrate.

Run via run.sh, which points openclaw-localtrace's outputDir at a fresh
throwaway directory and restarts the Gateway first.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid

SESSION_A_TURNS = [
    "Use the web_search tool with query \"top technology news today\", "
    "count 5, freshness \"day\", and tell me what you find. Just that one "
    "search for now.",
    "That's too generic to put in a briefing. Before we try anything "
    "else, run that exact same web_search call again, verbatim -- same "
    "query \"top technology news today\", same count 5, same freshness "
    "\"day\" -- to see if the results are any different this time.",
]

SESSION_B_TURNS = [
    "Use the web_search tool with query \"top technology news today\", "
    "count 5, freshness \"day\" to see what's out there. Just search, "
    "don't save anything yet.",
    "Save what you found to a file named briefing.md in the working "
    "directory.",
    "Now run that exact same web_search call again, verbatim -- same "
    "query \"top technology news today\", same count 5, same freshness "
    "\"day\" -- to double check the briefing is still accurate before I "
    "send it out.",
    "Also search for today's top story specifically, phrased however "
    "reads best to you, as a supplementary check before I finalize this.",
    "One more search on the same story, phrased differently again, just "
    "to triangulate before we're done.",
]


def _run_turn(agent: str, session_key: str, message: str) -> None:
    proc = subprocess.run(
        [
            "openclaw", "agent",
            "--agent", agent,
            "--session-key", session_key,
            "--message", message,
            "--json",
        ],
        capture_output=True, text=True, timeout=180,
    )
    stdout = proc.stdout.strip()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"   \033[31m[error parsing CLI output]\033[0m {stdout[:500]}")
        if proc.stderr.strip():
            print(f"   \033[31m{proc.stderr.strip()[:500]}\033[0m")
        return

    if data.get("ok") is False:
        print(f"   \033[31m[cli error] {data.get('error', {}).get('message')}\033[0m")
        return

    agent_meta = data.get("result", {}).get("meta", {}).get("agentMeta", {})
    tools = agent_meta.get("terminalReceipt", {}).get("successfulToolNames", [])
    if tools:
        print(f"   \033[2m[tools] {', '.join(tools)}\033[0m")

    payloads = data.get("result", {}).get("payloads", [])
    text = payloads[0].get("text", "") if payloads else ""
    for line in text.strip().splitlines():
        print(f"   {line}")


def _run_session(label: str, agent: str, session_key: str, turns: list[str]) -> None:
    print(f"\n\033[1;36m{'=' * 72}\033[0m")
    print(f"\033[1;36m  {label}\033[0m")
    print(f"\033[1;36m{'=' * 72}\033[0m")

    for i, message in enumerate(turns, 1):
        print(f"\n\033[1;33m>> turn {i}:\033[0m {message}")
        _run_turn(agent, session_key, message)

    print(f"\n\033[1;32m[{label}: done]\033[0m")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="agentsmith", help="OpenClaw agent id to run turns as")
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:8]
    _run_session(
        "Session A -- quick search, re-checked once, left unresolved",
        args.agent, f"agent:{args.agent}:demo-briefing-a-{run_id}", SESSION_A_TURNS,
    )
    _run_session(
        "Session B -- search, save, recheck, cross-check twice",
        args.agent, f"agent:{args.agent}:demo-briefing-b-{run_id}", SESSION_B_TURNS,
    )


if __name__ == "__main__":
    main()
