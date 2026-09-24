"""A tiny "research assistant with delegation" built on Hermes, the
redundo demo app for the openinference adapter (Hermes exports via
OpenInference semantic conventions). Drives two real, single-invocation
`hermes -z "..."` conversations and prints each one's reply.

Each session is one combined prompt, not several separate `--resume`
turns. That's a deliberate, verified choice, not a style preference: a
real multi-turn Hermes conversation driven via separate CLI invocations
(one process, and one OTel trace, per turn) hits a currently-tracked bug
in redundo's openinference adapter, where step numbering restarts at
zero for every trace even when several traces share one real task_id,
silently colliding on (task_id, step_index) and corrupting lineage-based
candidate-pair detection for that task. See this demo's README for the
full explanation and the tracked follow-up. Keeping each session to one
`-z` call keeps everything in a single trace, which does not hit that
bug, and Hermes genuinely can and does call several tools in sequence
within one turn, so this loses none of the "multi-step workflow" story.

Session A asks for a research topic and lets the agent delegate it to
two subagents via Hermes's real `delegate_task` tool, a real,
overlapping-topic multi-agent workflow (confirmed live: Hermes batches
both subagent goals into one `delegate_task` call, and both subagents'
own tool calls run for real, concurrently). Two subagents working in
parallel are lineage *siblings*, not one an ancestor of the other, so
redundo correctly never flags their overlapping calls against each
other here, that would be flagging normal fan-out as waste. Catching
this kind of cross-agent overlap is exactly what the `cross_task_redundancy`
bucket exists for. With redundo's own task_id-per-span and
parent_task_id fixes merged (see this demo's README), this session now
genuinely populates `cross_task_redundancy` on a live run, not just a
"what this app is building toward" placeholder.

Session B is a plain, single-agent, sequential turn: a calculation,
repeated verbatim with nothing intervening, then a save-and-recheck loop
(a real file write via `execute_code`), then a rephrased follow-up,
the same proven confirmed_waste/likely_legitimate/near_duplicate shape
as the other two demo apps in this package, all within one lineage
thread so redundo's ancestor-walk candidate search actually applies.

Run via run.sh, which points hermes-otel's fixed OTLP endpoint
(~/.hermes/hermes_otel.yaml) at a real `redundo collect` receiver first.
"""

from __future__ import annotations

import subprocess

SESSION_A_PROMPT = (
    "Research the environmental impact of electric vehicles by "
    "delegating two subtasks to subagents: one subagent should research "
    "battery manufacturing impact, and a second subagent should research "
    "the impact of EV battery production on the environment. Just give "
    "me a one paragraph summary of each when they're done."
)

SESSION_B_PROMPT = (
    "Do the following in order, as separate steps, each using code "
    "execution:\n"
    "1. Calculate 47 * 89 and show me the result.\n"
    "2. Run that exact same calculation again, verbatim, to confirm it's "
    "consistent. Don't skip this even though you already know the "
    "answer, I want to see it re-run.\n"
    "3. Save that result to a file named calc.txt in the working "
    "directory.\n"
    "4. Run that exact same calculation once more, verbatim, to double "
    "check the saved file is accurate.\n"
    "5. Also calculate 47 * 90 the same way, as a nearby comparison "
    "point.\n"
    "Report back what each step produced."
)


def _run_prompt(message: str) -> str:
    result = subprocess.run(
        ["hermes", "-z", message, "--yolo"], capture_output=True, text=True, timeout=300,
    )
    return result.stdout.strip()


def _run_session(label: str, prompt: str) -> None:
    print(f"\n\033[1;36m{'=' * 72}\033[0m")
    print(f"\033[1;36m  {label}\033[0m")
    print(f"\033[1;36m{'=' * 72}\033[0m")
    print(f"\n\033[1;33m>> prompt:\033[0m {prompt}")

    reply = _run_prompt(prompt)
    for line in reply.splitlines():
        print(f"   {line}")

    print(f"\n\033[1;32m[{label}: done]\033[0m")


def main() -> None:
    _run_session("Session A: research with real subagent delegation", SESSION_A_PROMPT)
    _run_session("Session B: sequential repeat-and-verify", SESSION_B_PROMPT)


if __name__ == "__main__":
    main()
