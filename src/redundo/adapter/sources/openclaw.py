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
2. cost_usd is an estimate, apportioned from the metrics signal -- never
   an exactly metered per-call figure, and only present at all when
   metrics documents were actually captured. OpenClaw's exporter emits a
   real cost estimate (`openclaw.cost.usd`, a cumulative Counter) split by
   (channel, model) attributes, but only on the *metrics* OTLP signal --
   never as a span attribute, and with no per-call granularity even there
   (a Counter has no `task_id`/`span_id` to join against; the exporter
   never populates exemplars either -- confirmed empirically, zero
   exemplars across a real capture with genuine spend). This adapter
   apportions each (channel, model) counter's total across every llm_call
   event sharing that (channel, model) -- weighted by token share
   (tokens_in + tokens_out) when available, split evenly otherwise --
   within the counter's own start/observed time window. That window
   boundary matters: `openclaw.cost.usd` resets to zero (and starts a new
   window) every time the exporting Gateway process restarts, confirmed
   directly during this feature's own development -- treating two
   generations of the same (channel, model) counter as one flat total
   would either double-count or silently drop a generation's spend
   depending on which point you picked. See `_apportion_cost` and
   docs/openclaw.md for the exact mechanism and what it still can't fix
   (there's no way to attribute cost to a specific llm_call more precisely
   than "this token's share of this window's total").
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
from ..base import AdapterSource, Detection
from ..otlp import (
    AGGREGATION_TEMPORALITY_CUMULATIVE,
    MetricPoint,
    Span,
    is_metrics_document,
    is_trace_document,
    parse_metric_points,
    parse_spans,
)

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

    # Cost apportionment (see _apportion_cost) -- 0/0 means no
    # openclaw.cost.usd metrics were captured at all, not that every llm_call
    # happened to cost nothing.
    cost_windows_found: int = 0
    llm_calls_with_apportioned_cost: int = 0
    llm_calls_apportioned_by_equal_split: int = 0

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
        if self.cost_windows_found == 0:
            out.append(
                "cost_usd is None for every record -- no openclaw.cost.usd metrics "
                "were found in this corpus (metrics documents weren't captured, or "
                "this source had none). See docs/openclaw.md."
            )
        else:
            out.append(
                f"cost_usd is an ESTIMATE for {self.llm_calls_with_apportioned_cost} "
                "llm_call record(s): each (channel, model) openclaw.cost.usd counter's "
                "total, for its own observed time window, apportioned across the "
                "llm_call events in that window by token share"
                + (
                    f" ({self.llm_calls_apportioned_by_equal_split} of those had no "
                    "token data to share by and were split evenly instead)"
                    if self.llm_calls_apportioned_by_equal_split
                    else ""
                )
                + ". Never an exactly metered per-call figure, and tool_call/"
                "tool_result records never get one -- see docs/openclaw.md."
            )
        return out


def convert_openclaw(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ConversionSummary]:
    """documents: a mix of OTLP traces and/or metrics JSON export documents
    (traces are required to produce any records at all; metrics are
    optional and, when present, feed cost apportionment -- see
    _apportion_cost)."""
    trace_docs = [d for d in documents if is_trace_document(d)]
    metrics_docs = [d for d in documents if is_metrics_document(d)]

    spans: list[Span] = []
    for document in trace_docs:
        spans.extend(parse_spans(document))
    summary = ConversionSummary(total_spans=len(spans))

    by_trace: dict[str, list[Span]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)

    records: list[dict[str, Any]] = []
    # (record, channel, llm_call span's start time) for every llm_call
    # produced below -- side channel for _apportion_cost, which needs the
    # channel attribute (never present on the llm_call span itself; see
    # _channel_of_trace) and the real nanosecond timestamp (not the
    # ISO-formatted one already written into the record) to place each call
    # within the right cost-counter time window.
    llm_call_index: list[tuple[dict[str, Any], str | None, int]] = []

    for trace_id, trace_spans in by_trace.items():
        channel = _channel_of_trace(trace_spans)
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
                llm_call_index.append((record, channel, span.start_time_unix_nano))
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

    if metrics_docs:
        metric_points: list[MetricPoint] = []
        for document in metrics_docs:
            metric_points.extend(parse_metric_points(document))
        generations = _parse_cost_generations(metric_points)
        _apportion_cost(llm_call_index, generations, summary)

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


def _channel_of_trace(trace_spans: list[Span]) -> str | None:
    """openclaw.channel for cost apportionment -- distinct from
    _workflow_of's per-span label. Confirmed empirically: no
    openclaw.model.call span carries openclaw.channel directly (only
    openclaw.run/openclaw.harness.run, its structural ancestors, do), and a
    whole trace shares exactly one channel in every real capture seen so
    far, so scanning the trace's spans for the first one that has it is
    both simpler and more robust here than walking any one span's specific
    ancestor chain.
    """
    for s in trace_spans:
        channel = s.attributes.get("openclaw.channel")
        if channel:
            return str(channel)
    return None


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


def _hash_json_attr(
    span: Span, attr: str, summary: ConversionSummary
) -> tuple[str, int, str, str | None]:
    """(content_hash, mask_count, content_basis, similarity_fingerprint)
    for a captureContent-only attribute that -- per the exporter's own
    test assertions -- is a JSON-stringified value (a message-parts array,
    or tool arguments/result). Falls back to opaque when absent, i.e.
    captureContent was off or this particular event carried nothing to
    capture.

    The fingerprint is only ever computed alongside real content, never
    for the opaque fallback -- a SimHash over a span's own arbitrary
    span_id string would be noise, not signal, and could produce a
    coincidentally small Hamming distance between two otherwise-unrelated
    opaque records, a false near-duplicate. `None` here means "not
    computed," same "absence is unknown, never a guess" discipline as
    every other optional metadata value in this module.
    """
    raw = span.attributes.get(attr)
    if raw is None:
        summary.records_with_opaque_content += 1
        digest, masks = _opaque_hash(span)
        return digest, masks, "opaque", None
    digest, masks = hashing.content_hash(raw, structured=True)
    fingerprint = hashing.similarity_fingerprint(raw, structured=True)
    summary.records_with_prompt_content += 1
    return digest, masks, "prompt", fingerprint


def _base_metadata(
    span: Span,
    masked_spans: int,
    content_basis: str,
    similarity_fingerprint: str | None = None,
) -> dict[str, Any]:
    metadata = {
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
    if similarity_fingerprint is not None:
        metadata["similarity_fingerprint"] = similarity_fingerprint
        metadata["similarity_spec"] = hashing.SIMILARITY_SPEC
    return metadata


def _llm_event(
    span: Span,
    task_id: str,
    step: int,
    parent_step: int | None,
    workflow: str | None,
    summary: ConversionSummary,
) -> dict[str, Any]:
    digest, masks, content_basis, fingerprint = _hash_json_attr(
        span, _INPUT_MESSAGES_ATTR, summary
    )

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

    metadata = _base_metadata(span, masks, content_basis, fingerprint)
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
    call_hash, call_masks, call_basis, call_fingerprint = _hash_json_attr(
        span, _TOOL_ARGS_ATTR, summary
    )
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
        "metadata": _base_metadata(span, call_masks, call_basis, call_fingerprint),
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


_COST_METRIC_NAME = "openclaw.cost.usd"
_COST_CHANNEL_ATTR = "openclaw.channel"
_COST_MODEL_ATTR = "openclaw.model"


@dataclass(frozen=True, slots=True)
class _CostGeneration:
    """One (channel, model) openclaw.cost.usd counter's final observed
    value for one *generation* -- the span of time between an exporter
    (re)start and either the next restart or the end of capture. A
    CUMULATIVE counter's own start_time_unix_nano changes when the
    exporting Gateway process restarts (confirmed directly against a real
    capture during this feature's development: the same channel/model
    pair's start_time jumped mid-session), so two points sharing a
    start_time are the same generation and two points with different
    start_times for the same (channel, model) are unrelated generations
    whose totals must both be counted, not collapsed into "just take the
    latest snapshot" (which would silently drop whatever the earlier
    generation had accumulated).
    """

    channel: str | None
    model: str | None
    start_time_unix_nano: int
    end_time_unix_nano: int  # the final point's own time_unix_nano
    total_usd: float


def _parse_cost_generations(metric_points: list[MetricPoint]) -> list[_CostGeneration]:
    latest_by_generation: dict[tuple[str | None, str | None, int], MetricPoint] = {}
    for point in metric_points:
        if point.name != _COST_METRIC_NAME or point.kind != "sum":
            continue
        if point.aggregation_temporality != AGGREGATION_TEMPORALITY_CUMULATIVE:
            continue  # a DELTA openclaw.cost.usd would need different handling; not seen in practice
        key = (
            point.attributes.get(_COST_CHANNEL_ATTR),
            point.attributes.get(_COST_MODEL_ATTR),
            point.start_time_unix_nano,
        )
        current = latest_by_generation.get(key)
        if current is None or point.time_unix_nano > current.time_unix_nano:
            latest_by_generation[key] = point

    return [
        _CostGeneration(
            channel=key[0],
            model=key[1],
            start_time_unix_nano=key[2],
            end_time_unix_nano=point.time_unix_nano,
            total_usd=point.value or 0.0,
        )
        for key, point in latest_by_generation.items()
    ]


def _apportion_cost(
    llm_call_index: list[tuple[dict[str, Any], str | None, int]],
    generations: list[_CostGeneration],
    summary: ConversionSummary,
) -> None:
    """Split each (channel, model) cost generation's total across the
    llm_call records that share that (channel, model) and belong to that
    generation's restart cycle -- by token share when the records in the
    window have token data, evenly otherwise. This is an estimate:
    openclaw.cost.usd has no per-call granularity to divide exactly (see
    module docstring point 2), so "this call's share of this generation's
    total" is the most precise claim the data supports.

    A generation's own start_time_unix_nano is when the exporting SDK
    started tracking that specific attribute combination as a distinct
    counter series -- not necessarily when the calls it covers began.
    Confirmed directly during this feature's development against a real
    capture: real llm_call spans landed both *before* their generation's
    own start_time (export/registration lag) and *after* its last observed
    export timestamp (the call happened, but the next periodic metrics
    flush hadn't occurred yet by the time this capture ended). Requiring a
    call to fall inside [start_time, last_export] missed both of those --
    confirmed to undercount roughly 12 of 13 real llm_call events in that
    capture. The boundary that's actually meaningful is *which restart
    cycle* a call belongs to: strictly before the *next* generation of the
    same (channel, model) begins, with no lower bound on the first
    generation and no upper bound on the last -- same open-ended-window
    idiom sources.claude_code uses for its own cross-signal time bucketing.
    """
    summary.cost_windows_found = len(generations)
    if not generations:
        return

    by_key: dict[tuple[str | None, str | None], list[_CostGeneration]] = {}
    for generation in generations:
        by_key.setdefault((generation.channel, generation.model), []).append(generation)

    for (channel_key, model_key), same_key_generations in by_key.items():
        same_key_generations.sort(key=lambda g: g.start_time_unix_nano)
        for index, generation in enumerate(same_key_generations):
            window_start = (
                float("-inf") if index == 0 else generation.start_time_unix_nano
            )
            window_end = (
                same_key_generations[index + 1].start_time_unix_nano
                if index + 1 < len(same_key_generations)
                else float("inf")
            )
            matching = [
                record
                for record, channel, start_ns in llm_call_index
                if record["model"] == model_key
                and channel == channel_key
                and window_start <= start_ns < window_end
            ]
            if not matching:
                continue

            total_tokens = sum(
                (record["tokens_in"] or 0) + (record["tokens_out"] or 0) for record in matching
            )
            for record in matching:
                if total_tokens > 0:
                    record_tokens = (record["tokens_in"] or 0) + (record["tokens_out"] or 0)
                    share = record_tokens / total_tokens
                    basis = "apportioned_from_metrics_by_tokens"
                else:
                    share = 1 / len(matching)
                    basis = "apportioned_from_metrics_equal_split"
                    summary.llm_calls_apportioned_by_equal_split += 1
                record["cost_usd"] = round(generation.total_usd * share, 6)
                record["metadata"]["cost_basis"] = basis
                summary.llm_calls_with_apportioned_cost += 1


_OBSERVATION_UNIT_ATTR = "openclaw.model_call.observation_unit"


class OpenClawSource(AdapterSource):
    name = "openclaw"

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if not is_trace_document(doc):
                continue
            for span in parse_spans(doc):
                if span.name.startswith("openclaw."):
                    return Detection("openclaw", f"span name {span.name!r}")
                if _OBSERVATION_UNIT_ATTR in span.attributes:
                    return Detection("openclaw", f"span attribute {_OBSERVATION_UNIT_ATTR!r}")
        return None

    def convert(self, documents: list[dict[str, Any]]):
        return convert_openclaw(documents)
