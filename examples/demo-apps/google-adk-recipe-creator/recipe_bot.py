"""A tiny "recipe creator" built on the real Google Agent Development Kit
(ADK), the redundo demo app for the openinference adapter's Google ADK
coverage (google-adk exports via openinference-instrumentation-google-adk,
converted by the same `openinference` adapter Hermes uses).

Built specifically to check two things confirmed only from reading
openinference-instrumentation-google-adk's own source
(`_wrappers.py`), never against a real capture, before this demo
existed:

1. `redundo.adapter.sources.openinference._workflow_of` now prefers an
   AGENT-kind ancestor's `agent.name` attribute over its own span name,
   specifically because ADK's own source names its AGENT-kind span
   `f"agent_run [{instance.name}]"` (decorated) while setting `agent.name`
   to the bare `instance.name` -- does a real capture actually show that
   decorated span name, confirming the attribute preference is a real
   fix and not a defensive no-op?
2. Does ADK ever emit the same "coarse whole-turn wrapper span plus a
   separate granular per-call span, both tagged
   openinference.span.kind=LLM" shape a real Hermes capture confirmed
   (see docs/openinference.md's investigation into llm_call events with
   no cost_usd) -- which would mean this is a general shape
   openinference.py needs to handle, not a Hermes-specific one -- or
   does ADK's own `_wrap_call_llm` (confirmed by source: sets
   `LLM_MODEL_NAME` directly on the one real completion span) avoid it
   entirely?

Session A drives a real sub-agent delegation: the root Recipe Creator
delegates to a Nutrition Analyst sub-agent (ADK's own `sub_agents=`
mechanism, confirmed real by the "hierarchical multi-agent" samples in
adk-python's own contributing/samples/multi_agent/sub_agents) for
ingredient-specific nutrition data, and is asked to re-check one lookup
verbatim (an exact-repeat candidate pair) plus a related ingredient (a
near-duplicate candidate pair) -- real, checkable tool calls to confirm
candidate-pair detection works end to end against this source too.

Session B stays with the root agent alone (explicitly told not to
delegate), repeating the same verbatim-then-related pattern with its
own direct tool access, so the same corpus also has a real "no
delegation, no AGENT-kind ancestor at all" case to confirm the `"main"`
default.

Each session is its own ADK session (real `session.id`, confirmed
mapped onto `session.id` -- never `gen_ai.conversation.id` -- by this
adapter's own docs/openinference.md, itself confirmed by reading
openinference-instrumentation-google-adk's source): ADK's session
concept already spans multiple `run_async` turns natively, unlike
openai-agents, so no manual trace-grouping workaround is needed here.

The get_nutrition_facts tool is a small, deterministic, in-process
lookup table, not a real API -- keeps every "verbatim repeat"
byte-identical and reproducible without a second API key.

Run via run.sh, which sets GOOGLE_API_KEY, starts a local `redundo
collect` OTLP receiver, and points this script's own OTLP exporter at
it directly.
"""

from __future__ import annotations

import asyncio

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types
from openinference.instrumentation.google_adk import GoogleADKInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_OTLP_ENDPOINT = "http://localhost:4318/v1/traces"

_tracer_provider = trace_sdk.TracerProvider()
_tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(_OTLP_ENDPOINT)))
GoogleADKInstrumentor().instrument(tracer_provider=_tracer_provider)

_MODEL = "gemini-2.5-flash"

# A small, deterministic, in-process table -- not a real API call, so
# every "verbatim repeat" is byte-identical by construction.
_NUTRITION_FACTS = {
    "chickpeas": {"calories_per_100g": 164, "protein_g": 9, "fiber_g": 8},
    "lentils": {"calories_per_100g": 116, "protein_g": 9, "fiber_g": 8},
    "quinoa": {"calories_per_100g": 120, "protein_g": 4, "fiber_g": 3},
}


def get_nutrition_facts(ingredient: str) -> dict:
    """Look up calories, protein, and fiber per 100g for an ingredient.

    Args:
        ingredient: The ingredient name, lowercase (e.g. "chickpeas").

    Returns:
        A dict of nutrition facts, or an error message if unknown.
    """
    return _NUTRITION_FACTS.get(ingredient.lower(), {"error": f"no data for {ingredient!r}"})


nutrition_analyst = Agent(
    name="nutrition_analyst",
    model=_MODEL,
    description="Looks up nutrition facts for specific ingredients.",
    instruction=(
        "You look up nutrition facts using the get_nutrition_facts tool. "
        "When asked to re-check an ingredient, call the tool again with "
        "the exact same ingredient string, verbatim -- do not reword it. "
        "Give a one sentence summary of whatever the tool returns."
    ),
    tools=[get_nutrition_facts],
)

recipe_creator = Agent(
    name="recipe_creator",
    model=_MODEL,
    description="Creates simple recipes and delegates nutrition questions.",
    instruction=(
        "You create short, simple recipes. For any question that needs "
        "real nutrition data for a specific ingredient, delegate to the "
        "nutrition_analyst sub-agent rather than guessing. When told "
        "explicitly not to delegate, use your own get_nutrition_facts "
        "tool access directly instead."
    ),
    sub_agents=[nutrition_analyst],
    tools=[get_nutrition_facts],
)

SESSION_A_TURNS = [
    "I want to make a chickpea salad. Get nutrition facts for "
    "'chickpeas' from the nutrition analyst.",
    "Before finalizing the recipe, have the nutrition analyst look up "
    "'chickpeas' again, verbatim, same ingredient string, to confirm "
    "the numbers.",
    "Also have them check 'lentils' as an alternative ingredient.",
]

SESSION_B_TURNS = [
    "Do not delegate for this. Look up nutrition facts for 'quinoa' "
    "yourself, using your own tool access.",
    "Run that same lookup again, verbatim, same ingredient string, "
    "just to double check.",
]


async def _run_session(label: str, app_name: str, turns: list[str]) -> None:
    print(f"\n\033[1;36m{'=' * 72}\033[0m")
    print(f"\033[1;36m  {label}\033[0m")
    print(f"\033[1;36m{'=' * 72}\033[0m")

    runner = InMemoryRunner(agent=recipe_creator, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id="demo_user"
    )
    for turn in turns:
        print(f"\n\033[1;33m>> turn:\033[0m {turn}")
        content = types.Content(role="user", parts=[types.Part(text=turn)])
        async for event in runner.run_async(
            user_id=session.user_id, session_id=session.id, new_message=content
        ):
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(part.text or "" for part in event.content.parts)
                if text:
                    print(f"   {text}")

    print(f"\n\033[1;32m[{label}: done]\033[0m")


async def main() -> None:
    await _run_session(
        "Session A: sub-agent delegation with a verbatim repeat",
        "recipe_creator_session_a", SESSION_A_TURNS,
    )
    await _run_session(
        "Session B: single-agent verbatim repeat, no delegation",
        "recipe_creator_session_b", SESSION_B_TURNS,
    )


if __name__ == "__main__":
    asyncio.run(main())
