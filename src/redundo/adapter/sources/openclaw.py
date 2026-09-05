"""OpenClaw's `@openclaw/diagnostics-otel` OTLP export -> redundo.analyzer's
Event schema.

Built against the exporter's own TypeScript source and test suite (not
just its docs), because a live capture attempt against a real running
Gateway (local Ollama provider, content capture forced on) produced a
connected, healthy exporter that nonetheless exported zero spans across
several real turns -- see docs/openclaw.md for the full account. Every
claim below is checked against `service-recorders-model.ts`,
`service-recorders-tools.ts`, `service-genai-content.ts`, and the literal
example payloads asserted in `service.test.ts`, not against prose.

Four things that aren't obvious from the code and are easy to silently
violate while extending this:

1. task_id is always the trace ID, never a real session/conversation id --
   and this is not a "fell back to trace_id because the good signal was
   missing this time" situation the way it is for OpenInference. OpenClaw's
   exporter *actively strips* every session/run/call identifier
   (`sessionKey`, `sessionId`, `runId`, `callId`, `toolCallId`'s
   session-adjacent siblings, ...) from every exported span attribute via
   a deny-list scrub (`redactOtelAttributes` / `DROPPED_OTEL_ATTRIBUTE_KEYS`
   in the exporter source), enforced by dedicated tests. There is no
   richer signal to fall back to, ever, from this signal alone -- trace_id
   is the ceiling, not a degraded case. `metadata.task_id_source` is still
   set to `"trace_id_fallback"` (the existing convention's value for "not
   a real conversation id"), but see docs/openclaw.md for why that framing
   undersells how structural this is here.
2. cost_usd is always None. OpenClaw's exporter does emit a real cost
   estimate (`openclaw.cost.usd`, a Counter), but only on the *metrics*
   OTLP signal -- never as a span attribute. This adapter (like every
   source in this package) only reads the traces and logs signals; the
   logs signal here carries only generic gateway log/security records,
   nothing model-call-shaped. Cost from this source is unreachable from
   what `redundo adapt` captures, not merely unobserved.
3. Only `openclaw.model.call` and `openclaw.tool.execution` spans become
   Events. `openclaw.harness.run` and `openclaw.run` are structural
   wrapper spans (their nearest analogue is OpenInference's AGENT/CHAIN
   kinds) -- skipped, counted, and used only for lineage-ancestor lookup
   and the `workflow` label. `openclaw.model.usage` is also skipped: it's
   a distinct span from `openclaw.model.call` in the exporter source and
   this adapter could not confirm from the available source/tests alone
   whether it ever represents API calls not already captured by
   `openclaw.model.call`, or is always redundant with it. Treating it as
   an additional llm_call risked double-counting a single real call;
   skipping it risks undercounting into blind spots. Skip-and-report was
   the choice that can't silently produce a wrong number either way.
4. Content (`gen_ai.input.messages`, `gen_ai.output.messages`,
   `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`) only exists
   when the operator has explicitly turned on
   `diagnostics.otel.captureContent` -- off by default. Without it, every
   record from this source degrades to `content_basis: "opaque"` (a hash
   of the span's own id, never coincidentally matching anything -- see
   `_opaque_hash`, same idiom `sources.claude_code` uses for its own
   no-content case).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import hashing
from ..otlp import Span, parse_spans

# Span name -> the event_type it becomes. Every other span name is a
# structural wrapper or an unmapped signal (see module docstring point 3)
# and is skipped, not guessed at.
_EVENT_TYPE_BY_SPAN_NAME = {
    "openclaw.model.call": "llm_call",
    "openclaw.tool.execution": "tool_call",
}
_HARNESS_SPAN_NAME = "openclaw.harness.run"

_INPUT_MESSAGES_ATTR = "gen_ai.input.messages"
_OUTPUT_MESSAGES_ATTR = "gen_ai.output.messages"
_TOOL_ARGS_ATTR = "gen_ai.tool.call.arguments"
_TOOL_RESULT_ATTR = "gen_ai.tool.call.result"

_MODEL_ATTRS = ("gen_ai.request.model", "openclaw.model")
_TOOL_NAME_ATTRS = ("gen_ai.tool.name", "openclaw.toolName")

_TOKENS_IN_ATTRS = ("gen_ai.usage.input_tokens",)
_TOKENS_IN_CACHE_ATTRS = (
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
)
_TOKENS_OUT_ATTRS = ("gen_ai.usage.output_tokens",)

_ERROR_ATTRS = ("openclaw.errorCategory", "error.type")
_BLOCKED_OUTCOME_VALUE = "blocked"


@dataclass
class ConversionSummary:
    total_spans: int = 0
    kept_spans: int = 0
    skipped_by_kind: dict[str, int] = field(default_factory=dict)

    total_records: int = 0
    records_with_prompt_content: int = 0
    records_with_opaque_content: int = 0

    hash_spec: str = hashing.HASH_SPEC

    def notes(self) -> list[str]:
        out: list[str] = []
        if self.skipped_by_kind:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped_by_kind.items()))
            out.append(
                f"skipped {sum(self.skipped_by_kind.values())} span(s) with a name "
                f"this adapter doesn't map to an event type ({detail})."
            )
        if self.total_records == 0:
            out.append("no records produced -- nothing below is meaningful.")
            return out
        out.append(
            f"{self.records_with_opaque_content}/{self.total_records} record(s) "
            f"({self.records_with_opaque_content / self.total_records:.0%}) have no "
            "observable content -- diagnostics.otel.captureContent was off (the "
            "default) for this corpus, or the specific event was one this adapter "
            "can't get content from either way. Their content_hash is opaque and "
            "cannot match anything (see metadata.content_basis)."
        )
        out.append(
            "task_id is always the OTLP trace ID for this source -- OpenClaw's "
            "exporter strips every session/run identifier from exported spans by "
            "design, not as a fallback. Repeats spanning more than one trace "
            "(multi-turn conversations) are not detectable from this signal. "
            "See docs/openclaw.md."
        )
        out.append(
            "cost_usd is always None for this source -- OpenClaw only exports cost "
            "on the metrics OTLP signal, which this adapter doesn't read (and which "
            "has no per-event granularity to read anyway). See docs/openclaw.md."
        )
        return out


def convert_openclaw(documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ConversionSummary]:
    """documents: one or more parsed OTLP traces JSON export documents."""
    spans: list[Span] = []
    for document in documents:
        spans.extend(parse_spans(document))
    summary = ConversionSummary(total_spans=len(spans))

    by_trace: dict[str, list[Span]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)

    records: list[dict[str, Any]] = []

    for trace_id, trace_spans in by_trace.items():
        span_by_id = {s.span_id: s for s in trace_spans}
        kept = [s for s in trace_spans if s.name in _EVENT_TYPE_BY_SPAN_NAME]
        for s in trace_spans:
            if s.name not in _EVENT_TYPE_BY_SPAN_NAME:
                summary.skipped_by_kind[s.name] = summary.skipped_by_kind.get(s.name, 0) + 1
        kept.sort(key=lambda s: s.start_time_unix_nano)
        summary.kept_spans += len(kept)

        step = 0
        last_step_of_span: dict[str, int] = {}
        # Real parent_span_id reflects call-stack nesting, not necessarily
        # turn-to-turn execution order -- unverified against a live
        # capture whether OpenClaw nests sequential model/tool calls under
        # each other or puts them all as flat siblings of one
        # openclaw.run span (see module docstring). This sibling-chaining
        # technique (borrowed from sources.openinference, which confirmed
        # the flat case happens for at least one real OTel-instrumented
        # agent framework) is a no-op if the real topology already nests
        # correctly, and a safety net if it doesn't -- but only beneath a
        # real *kept* ancestor somewhere further up the chain (an actual
        # converted llm_call/tool_call, not the flat wrapper itself). A
        # span is chained to the nearest earlier sibling under that kept
        # ancestor only when their [start, end) intervals don't overlap --
        # non-overlap is real evidence of "next sequential step," not proof
        # by itself, but overlapping siblings are always left attached
        # directly to the ancestor, preserving genuine parallel fan-out
        # either way. A flat group with no kept ancestor anywhere above it
        # (e.g. the very first turn in a trace) is deliberately left
        # unlinked rather than guessed at -- chaining across two genuinely
        # independent top-level flame graphs in the same trace would be a
        # wrong finding, not a conservative one.
        chain_tail: dict[str, tuple[int, int, int]] = {}  # kept ancestor id -> (start, end, step)

        for span in kept:
            event_type = _EVENT_TYPE_BY_SPAN_NAME[span.name]
            ancestor_id = _resolve_kept_ancestor_span_id(span, span_by_id, last_step_of_span)
            span_end = span.end_time_unix_nano or span.start_time_unix_nano

            if ancestor_id is None:
                parent_step = None
            else:
                tail = chain_tail.get(ancestor_id)
                if tail is not None and span.start_time_unix_nano >= tail[1]:
                    parent_step = tail[2]
                else:
                    parent_step = last_step_of_span[ancestor_id]

            workflow = _workflow_of(span, span_by_id)

            if event_type == "llm_call":
                record = _llm_event(span, trace_id, step, parent_step, workflow, summary)
                records.append(record)
                last_step_of_span[span.span_id] = step
                _extend_chain_tail(chain_tail, ancestor_id, span.start_time_unix_nano, span_end, step)
                step += 1
            else:
                call, result = _tool_events(span, trace_id, step, parent_step, workflow, summary)
                records.append(call)
                call_step = step
                last_step_of_span[span.span_id] = call_step
                step += 1
                final_step = call_step
                if result is not None:
                    result["step_index"] = step
                    result["parent_id"] = call_step
                    records.append(result)
                    last_step_of_span[span.span_id] = step
                    final_step = step
                    step += 1
                _extend_chain_tail(chain_tail, ancestor_id, span.start_time_unix_nano, span_end, final_step)

    summary.total_records = len(records)
    return records, summary


def _resolve_kept_ancestor_span_id(
    span: Span, span_by_id: dict[str, Span], last_step_of_span: dict[str, int]
) -> str | None:
    """Walk the real parent_span_id chain to the nearest ancestor span that
    was itself kept (converted into a record), skipping structural wrapper
    spans (openclaw.harness.run, openclaw.run, and any other unmapped
    span). Same technique as sources.openinference's helper of the same
    name -- see that module for the fuller explanation.
    """
    current_id = span.parent_span_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        if current_id in last_step_of_span:
            return current_id
        parent = span_by_id.get(current_id)
        if parent is None:
            return None
        current_id = parent.parent_span_id
    return None


def _extend_chain_tail(
    chain_tail: dict[str, tuple[int, int, int]],
    ancestor_id: str | None,
    start: int,
    end: int,
    step: int,
) -> None:
    if ancestor_id is None:
        return
    tail = chain_tail.get(ancestor_id)
    if tail is None or end >= tail[1]:
        chain_tail[ancestor_id] = (start, end, step)


def _workflow_of(span: Span, span_by_id: dict[str, Span]) -> str | None:
    """Best-effort segmentation label: the nearest openclaw.harness.run
    ancestor's `openclaw.harness.id`, falling back to the span's own
    `openclaw.channel` attribute, falling back to None. Unlike task_id,
    workflow has no "never guess" constraint -- it's inherently an
    approximate label.
    """
    current_id = span.parent_span_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        parent = span_by_id.get(current_id)
        if parent is None:
            break
        if parent.name == _HARNESS_SPAN_NAME:
            harness_id = parent.attributes.get("openclaw.harness.id")
            return str(harness_id) if harness_id else parent.name
        current_id = parent.parent_span_id
    channel = span.attributes.get("openclaw.channel")
    return str(channel) if channel else None


def _first_present(attributes: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if attributes.get(key) is not None:
            return attributes[key]
    return None


def _sum_present(attributes: dict[str, Any], keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        value = attributes.get(key)
        if value is not None:
            total += int(value)
    return total


def _outcome(attributes: dict[str, Any], has_ended: bool) -> str | None:
    if _first_present(attributes, _ERROR_ATTRS) is not None:
        return "error"
    if attributes.get("openclaw.outcome") == _BLOCKED_OUTCOME_VALUE:
        return "error"
    if has_ended:
        return "ok"
    return None


def _iso_timestamp(unix_nano: int) -> str:
    return datetime.fromtimestamp(unix_nano / 1e9, tz=timezone.utc).isoformat()


def _opaque_hash(span: Span) -> tuple[str, int]:
    """A content_hash derived from the span's own id, not its content --
    unique per span by construction, so it can never coincidentally equal
    another record's hash. Same idiom as sources.claude_code's helper of
    the same name.
    """
    return hashing.content_hash(span.span_id, structured=False)


def _hash_json_attr(span: Span, attr: str, summary: ConversionSummary) -> tuple[str, int, str]:
    """(content_hash, mask_count, content_basis) for a captureContent-only
    attribute that -- per the exporter's own test assertions -- is a
    JSON-stringified value (a message-parts array, or tool
    arguments/result). Falls back to opaque when absent, i.e. captureContent
    was off or this particular event carried nothing to capture.
    """
    raw = span.attributes.get(attr)
    if raw is None:
        summary.records_with_opaque_content += 1
        digest, masks = _opaque_hash(span)
        return digest, masks, "opaque"
    digest, masks = hashing.content_hash(raw, structured=True)
    summary.records_with_prompt_content += 1
    return digest, masks, "prompt"


def _base_metadata(span: Span, masked_spans: int, content_basis: str) -> dict[str, Any]:
    return {
        "hash_spec": hashing.HASH_SPEC,
        "masked_spans": masked_spans,
        "otlp_span_id": span.span_id,
        "otlp_trace_id": span.trace_id,
        # Always trace_id_fallback for this source -- see module docstring
        # point 1 for why there is no better signal to have fallen back
        # *from*.
        "task_id_source": "trace_id_fallback",
        "content_basis": content_basis,
    }


def _llm_event(
    span: Span,
    task_id: str,
    step: int,
    parent_step: int | None,
    workflow: str | None,
    summary: ConversionSummary,
) -> dict[str, Any]:
    digest, masks, content_basis = _hash_json_attr(span, _INPUT_MESSAGES_ATTR, summary)

    response_hash = None
    output_raw = span.attributes.get(_OUTPUT_MESSAGES_ATTR)
    if output_raw is not None:
        response_hash, _ = hashing.content_hash(output_raw, structured=True)

    model = _first_present(span.attributes, _MODEL_ATTRS)
    tokens_in_raw = _first_present(span.attributes, _TOKENS_IN_ATTRS)
    tokens_in = None
    if tokens_in_raw is not None:
        tokens_in = int(tokens_in_raw) + _sum_present(span.attributes, _TOKENS_IN_CACHE_ATTRS)
    tokens_out_raw = _first_present(span.attributes, _TOKENS_OUT_ATTRS)

    metadata = _base_metadata(span, masks, content_basis)
    if response_hash is not None:
        metadata["response_hash"] = response_hash

    return {
        "task_id": task_id,
        "step_index": step,
        "event_type": "llm_call",
        "name": model or span.name,
        "content_hash": digest,
        "tokens_in": tokens_in,
        "tokens_out": int(tokens_out_raw) if tokens_out_raw is not None else None,
        "outcome": _outcome(span.attributes, span.end_time_unix_nano is not None),
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        # Always None -- see module docstring point 2.
        "cost_usd": None,
        "model": model,
        "parent_id": parent_step,
        "workflow": workflow,
        "metadata": metadata,
    }


def _tool_events(
    span: Span,
    task_id: str,
    step: int,
    parent_step: int | None,
    workflow: str | None,
    summary: ConversionSummary,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    call_hash, call_masks, call_basis = _hash_json_attr(span, _TOOL_ARGS_ATTR, summary)
    tool_name = _first_present(span.attributes, _TOOL_NAME_ATTRS) or span.name
    has_ended = span.end_time_unix_nano is not None
    # A blocked call never executes, so it can never produce a tool_result
    # -- unlike an ordinary call/result pair (where outcome lives on the
    # result, per this package's convention), "blocked" would otherwise be
    # silently indistinguishable from any other call with an unobserved
    # result. Surface it directly on the call, the one place it can go.
    blocked = span.attributes.get("openclaw.outcome") == _BLOCKED_OUTCOME_VALUE

    call = {
        "task_id": task_id,
        "step_index": step,
        "event_type": "tool_call",
        "name": tool_name,
        "content_hash": call_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": "error" if blocked else None,
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": parent_step,
        "workflow": workflow,
        "metadata": _base_metadata(span, call_masks, call_basis),
    }

    raw_result = span.attributes.get(_TOOL_RESULT_ATTR)
    if raw_result is None:
        # No result attribute at all -- captureContent was off, or the
        # call errored/was blocked before producing output. No tool_result
        # event, same discipline every other source in this package
        # follows: an absent event reads as UNKNOWN downstream, never a
        # fabricated hash that could look like a real match or mismatch.
        return call, None

    result_hash, result_masks = hashing.content_hash(raw_result, structured=True)
    summary.records_with_prompt_content += 1

    result = {
        "task_id": task_id,
        "step_index": None,  # filled in by convert_openclaw() once the call's step is known
        "event_type": "tool_result",
        "name": tool_name,
        "content_hash": result_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": _outcome(span.attributes, has_ended),
        "timestamp": _iso_timestamp(span.end_time_unix_nano or span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": None,  # filled in by convert_openclaw()
        "workflow": workflow,
        "metadata": _base_metadata(span, result_masks, "prompt"),
    }
    return call, result
