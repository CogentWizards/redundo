"""Figure out which source produced a captured OTLP corpus, so the CLI can
"just point it at your traces directory" without the user having to name
the source themselves.

Detection order, each check grounded in something actually verified
against real captured data or real source code (see each source's
docs/*.md), not assumed:

1. Any span named `claude_code.*` -> Claude Code. Cowork never produces
   trace spans in its documented configuration, and OpenInference spans
   are never named this way, so this is unambiguous by itself.
2. Any span named `openclaw.*` -> OpenClaw. Same reasoning: no other
   supported source uses this prefix. This only catches OpenClaw's default
   span naming -- an operator who has opted into
   `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` gets spans
   named `"<gen_ai.operation.name> <model>"` instead, which is genuinely
   ambiguous with other gen_ai-semconv-instrumented sources and isn't
   guessed at here -- see step 3's fallback and docs/openclaw.md.
3. Any span carrying `openinference.span.kind` -> OpenInference (Hermes,
   or anything else instrumented with an OpenInference-compatible
   library -- see docs/openinference.md for what that covers and doesn't).
4. Any span carrying `openclaw.model_call.observation_unit` -> OpenClaw.
   Catches the gen_ai_latest_experimental-named case from step 2: this
   attribute is present on every OpenClaw model-call span regardless of
   naming mode (confirmed against the exporter's own source), and no other
   supported source sets it.
5. Otherwise (a logs-only corpus, no trace spans at all): Claude Code and
   Cowork both emit an overlapping set of logs-signal event names
   (`user_prompt`, `tool_result`, `api_request`, ...), so the event shape
   alone is genuinely ambiguous -- confirmed empirically, not assumed
   (see docs/claude-code.md and docs/cowork.md). Disambiguated by the
   OTLP resource-level `service.name` attribute instead
   (`resource.attributes`, attached once per resourceLogs entry,
   describing the emitting process -- "claude-code" or "cowork",
   confirmed against real captures / the Cowork monitoring reference).
   Falls back to Claude-Code-only event names (`mcp_server_connection`,
   `permission_mode_changed`, `auth` -- not in Cowork's documented event
   list) if `service.name` is ever missing.

Detection failure is reported, never guessed past -- see DetectionError.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .otlp import document_resource_attributes, is_log_document, is_trace_document, parse_log_records, parse_spans


class Source(str, Enum):
    OPENINFERENCE = "openinference"
    CLAUDE_CODE = "claude-code"
    COWORK = "cowork"
    OPENCLAW = "openclaw"


class DetectionError(ValueError):
    pass


# Event names Cowork's monitoring reference documents in full -- an event
# outside this set appearing in a logs-only corpus means it isn't Cowork.
_COWORK_EVENT_NAMES = frozenset(
    {"user_prompt", "assistant_response", "tool_result", "api_request", "api_error", "tool_decision"}
)
# Observed on real Claude Code captures, never documented for Cowork.
_CLAUDE_CODE_ONLY_EVENT_NAMES = frozenset(
    {"mcp_server_connection", "permission_mode_changed", "auth"}
)

_OPENINFERENCE_KIND_ATTR = "openinference.span.kind"
_OPENCLAW_OBSERVATION_UNIT_ATTR = "openclaw.model_call.observation_unit"


@dataclass
class Detection:
    source: Source
    reason: str


def detect_source(documents: list[dict[str, Any]]) -> Detection:
    trace_docs = [d for d in documents if is_trace_document(d)]
    log_docs = [d for d in documents if is_log_document(d)]

    for doc in trace_docs:
        for span in parse_spans(doc):
            if span.name.startswith("claude_code."):
                return Detection(Source.CLAUDE_CODE, f"span name {span.name!r}")
            if span.name.startswith("openclaw."):
                return Detection(Source.OPENCLAW, f"span name {span.name!r}")
            if _OPENINFERENCE_KIND_ATTR in span.attributes:
                return Detection(
                    Source.OPENINFERENCE, f"span attribute {_OPENINFERENCE_KIND_ATTR!r}"
                )
            if _OPENCLAW_OBSERVATION_UNIT_ATTR in span.attributes:
                return Detection(
                    Source.OPENCLAW, f"span attribute {_OPENCLAW_OBSERVATION_UNIT_ATTR!r}"
                )

    for doc in trace_docs + log_docs:
        for resource_attrs in document_resource_attributes(doc):
            service_name = resource_attrs.get("service.name")
            if service_name == "claude-code":
                return Detection(Source.CLAUDE_CODE, "resource service.name='claude-code'")
            if service_name == "cowork":
                return Detection(Source.COWORK, "resource service.name='cowork'")

    event_names: set[str] = set()
    for doc in log_docs:
        for rec in parse_log_records(doc):
            name = rec.attributes.get("event.name")
            if isinstance(name, str):
                event_names.add(name)
    if event_names:
        if event_names & _CLAUDE_CODE_ONLY_EVENT_NAMES:
            return Detection(
                Source.CLAUDE_CODE,
                f"event name(s) {sorted(event_names & _CLAUDE_CODE_ONLY_EVENT_NAMES)} "
                "not in Cowork's documented event set",
            )
        if event_names <= _COWORK_EVENT_NAMES:
            return Detection(
                Source.COWORK,
                "logs-only corpus with only Cowork-documented event names, no "
                "service.name and no Claude-Code-only event present",
            )

    raise DetectionError(
        "could not determine which source produced this corpus -- found "
        f"{len(trace_docs)} trace document(s) and {len(log_docs)} log document(s), "
        "but none carried a recognizable span name, openinference.span.kind "
        "attribute, resource service.name, or logs-signal event.name. Pass "
        "--source explicitly (openinference, claude-code, cowork, or openclaw)."
    )
