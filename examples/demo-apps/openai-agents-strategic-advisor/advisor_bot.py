"""A tiny "strategic advisor" built on the real OpenAI Agents SDK, the
redundo demo app for the openinference adapter's OpenAI Agents coverage
(openai-agents exports via openinference-instrumentation-openai-agents,
converted by the same `openinference` adapter Hermes uses).

Built specifically to check two things this package could only assume
from reading the instrumentation library's own source, not from a real
capture, before this demo existed:

1. Does `_workflow_of`'s new `agent.name`-attribute preference (added
   after Google ADK's own AGENT-kind span turned out to decorate its
   span name, "agent_run [<name>]") change anything here, or is it a
   no-op the way the source code suggested (openai-agents' own AGENT-kind
   span already uses the bare agent name as its span name too)?
2. Does openai-agents ever emit the same "coarse whole-turn wrapper span
   plus a separate granular per-call span, both tagged
   openinference.span.kind=LLM" shape confirmed on a real Hermes capture
   (see docs/openinference.md and this repo's own investigation into why
   some llm_call events showed no cost_usd) -- which would mean
   openinference.py needs a general, not Hermes-specific, fix -- or is
   each real API call here already just one span, one record?

Session A drives a real handoff: the root Strategic Advisor hands off to
a Market Analyst specialist for sector-specific market data, and is
asked to re-run one lookup verbatim (an exact-repeat candidate pair) plus
a related-but-different lookup (a near-duplicate candidate pair) --
real, checkable tool calls to also confirm candidate-pair detection
works end to end against this source, not just the workflow/model
question above.

Session B stays with the root agent alone (explicitly told not to
delegate), repeating the same verbatim-then-related pattern with its
own direct tool access, so the same corpus also has a real "no
handoff, no AGENT-kind ancestor at all" case to confirm the `"main"`
default (see redundo.adapter.sources.openinference._workflow_of).

Every session is wrapped in one `trace()` context deliberately:
openinference-instrumentation-openai-agents never sets
gen_ai.conversation.id/session.id (confirmed by reading its own
_processor.py source, not assumed), so without a shared trace, each
turn would land in its own trace_id and therefore its own task_id --
losing all cross-turn candidate-pair detection within one session. One
trace per session is what keeps a session's own turns groupable as one
task, the same role Hermes's own real session id plays for that source.

The get_market_data tool is a small, deterministic, in-process lookup
table, not a real API -- keeps every "verbatim repeat" byte-identical
and reproducible without a second API key.

Run via run.sh, which sets OPENAI_API_KEY, starts a local `redundo
collect` OTLP receiver, and points this script's own OTLP exporter at
it directly (openai-agents has no OTEL_EXPORTER_OTLP_ENDPOINT
auto-config the way Claude Code does, so this file wires the exporter
itself).
"""

from __future__ import annotations

import asyncio

from agents import Agent, Runner, trace
from agents.decorators import tool
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_OTLP_ENDPOINT = "http://localhost:4318/v1/traces"

_tracer_provider = trace_sdk.TracerProvider()
_tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(_OTLP_ENDPOINT)))
OpenAIAgentsInstrumentor().instrument(tracer_provider=_tracer_provider)

_MODEL = "gpt-4o-mini"

# A small, deterministic, in-process table -- not a real API call, so
# every "verbatim repeat" is byte-identical by construction, the same
# discipline claude-agent-sdk-code-review's own module docstring
# documents for its Bash-command reuse rule.
_MARKET_DATA = {
    "electric vehicles": {
        "market_size_usd_billion": 500, "cagr_percent": 18,
        "top_players": ["Tesla", "BYD", "Volkswagen"],
    },
    "renewable energy": {
        "market_size_usd_billion": 1200, "cagr_percent": 12,
        "top_players": ["NextEra Energy", "Iberdrola", "Orsted"],
    },
}


@tool
def get_market_data(sector: str) -> dict:
    """Look up market size, growth rate, and top players for a sector.

    Args:
        sector: The sector name, lowercase (e.g. "electric vehicles").
    """
    return _MARKET_DATA.get(sector.lower(), {"error": f"no data for sector {sector!r}"})


market_analyst = Agent(
    name="Market Analyst",
    model=_MODEL,
    instructions=(
        "You research market data using the get_market_data tool. When "
        "asked to re-check a sector, call the tool again with the exact "
        "same sector string, verbatim -- do not reword it. Give a one "
        "sentence summary of whatever the tool returns."
    ),
    tools=[get_market_data],
)

strategic_advisor = Agent(
    name="Strategic Advisor",
    model=_MODEL,
    instructions=(
        "You give one-paragraph business strategy recommendations. For "
        "any question that needs real market data for a specific sector, "
        "hand off to the Market Analyst rather than guessing. When told "
        "explicitly not to delegate, use your own get_market_data tool "
        "access directly instead."
    ),
    handoffs=[market_analyst],
    tools=[get_market_data],
)

SESSION_A_TURNS = [
    "I'm evaluating whether to enter the electric vehicles sector. Get "
    "market data for 'electric vehicles' from the Market Analyst.",
    "Before finalizing, have the Market Analyst look up 'electric "
    "vehicles' again, verbatim, same sector string, to confirm the "
    "numbers are stable.",
    "Also have them check 'renewable energy' as a comparison sector.",
]

SESSION_B_TURNS = [
    "Do not delegate for this. Look up market data for 'renewable "
    "energy' yourself, using your own tool access.",
    "Run that same lookup again, verbatim, same sector string, just to "
    "double check.",
]


async def _run_session(label: str, turns: list[str]) -> None:
    print(f"\n\033[1;36m{'=' * 72}\033[0m")
    print(f"\033[1;36m  {label}\033[0m")
    print(f"\033[1;36m{'=' * 72}\033[0m")

    current_agent = strategic_advisor
    inputs: list[dict] = []
    with trace(label):
        for turn in turns:
            print(f"\n\033[1;33m>> turn:\033[0m {turn}")
            inputs.append({"role": "user", "content": turn})
            result = await Runner.run(current_agent, input=inputs)
            print(f"   {result.final_output}")
            inputs = result.to_input_list()
            # last_agent, not current_agent -- that attribute only exists
            # on RunResultStreaming (Runner.run_streamed's return type);
            # Runner.run() returns a plain RunResult, whose equivalent
            # property is last_agent (confirmed by reading result.py in
            # the actual installed package, not assumed from an example
            # that happened to use the streaming variant).
            current_agent = result.last_agent

    print(f"\n\033[1;32m[{label}: done]\033[0m")


async def main() -> None:
    await _run_session("Session A: delegation with a verbatim repeat", SESSION_A_TURNS)
    await _run_session("Session B: single-agent verbatim repeat, no handoff", SESSION_B_TURNS)


if __name__ == "__main__":
    asyncio.run(main())
