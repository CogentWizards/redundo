"""`openclaw-localtrace` (https://github.com/CogentWizards/openclaw-localtrace)
-> redundo.analyzer's Event schema.

This is the sibling adapter to `sources.openclaw`, for a *different*
exporter: a standalone plugin built specifically because
`@openclaw/diagnostics-otel` strips every session/run identifier and
never reports a write/mutation signal (see docs/openclaw.md). Every claim
below is checked against that plugin's own TypeScript source
(`src/spans.ts`, `src/metrics.ts`) and a real capture from a live
Gateway, not against its README alone -- see docs/openclaw-localtrace.md
for the full account, including two real bugs (a stale runtime handle, a
mis-nested content span) found only by that live capture and fixed
upstream before this adapter was written.

Four things that aren't obvious from the plugin's output and are easy to
silently violate while extending this:

1. task_id is the real sessionId when the operator has turned on the
   plugin's own `captureIdentifiers` config (off by default) -- a
   genuine conversation id, not a per-trace one, so redundancy spanning
   more than one turn is detectable for the first time from an OpenClaw
   source. Without `captureIdentifiers`, there is no session attribute
   on any span at all, and this degrades to trace_id like every other
   fallback case in this package -- not a partial signal, a total
   absence, same "ceiling vs. fallback" distinction sources.openclaw
   draws for its own ever-present trace_id.
2. `openclaw-localtrace.llm.call` is scoped to the whole *run* (turn),
   not to one individual `model.call` -- confirmed against a real capture
   with a 13-iteration tool loop inside one run that produced exactly
   ONE `llm.call` span, whose own [start, end) interval contains all 13
   `model.call` spans (opens before the first model_call_started, closes
   after the last model_call_ended). An earlier version of this adapter
   assumed a 1:1 pairing (llm_input/llm_output bracketing each individual
   model.call, matching an early, smaller-scale live test) and required
   matching counts before pairing anything -- which meant a real run with
   N model.call spans and 1 llm.call span got NO pairing at all, silently
   discarding content and token data for every single one of that run's
   llm_call events. Fixed: each `llm.call` span is paired with the LAST
   `model.call` span it temporally contains (`_pair_llm_call_spans`) --
   the one whose response most plausibly produced the captured
   `assistantTexts` -- since `llm_output`'s content/usage describes that
   final attempt, not each intermediate tool-loop step. Every earlier
   model.call in the same run keeps its ids/timing but degrades
   content_hash to `content_basis: "opaque"` -- not a gap, an accurate
   reflection of what this signal was ever designed to expose. A
   `model.call` outside every llm.call's interval (permission not
   granted, `captureContent` off, or no llm.call at all) degrades the
   same way.
3. cost_usd comes from `openclaw.turn.cost.usd`, a Gauge with one point
   per *turn* (not a cumulative Counter the way sources.openclaw's
   `openclaw.cost.usd` is) -- genuinely simpler to apportion because
   there's no restart-generation ambiguity to resolve, but it only
   carries a session-identifying attribute at all when
   `captureIdentifiers` is on, same gate as point 1. Each point is
   apportioned across the one *run*'s own llm_call events (by token
   share, evenly otherwise) -- the run whose own [start, end) window
   ends most recently before the point's own timestamp, for that same
   session. A corpus without `captureIdentifiers` gets no cost data at
   all from this source, ever -- not an occasional gap.
4. `openclaw-localtrace.run` is a structural wrapper span (one per agent
   turn), like sources.openclaw's `openclaw.run`/`openclaw.harness.run`
   -- skipped, never converted, used only for the `workflow` label and
   the run-window boundaries point 3 needs. Unlike sources.openclaw,
   there is no ancestor-chaining fallback needed anywhere in this
   module: this plugin's parent_span_id topology is real and direct by
   construction (every model.call/llm.call/tool.execution span is
   literally a child of its run span, confirmed live), so `parent_id` is
   simply None for every event this source produces -- there is no
   nested-call topology in this plugin's v1 scope to represent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import hashing
from ..base import AdapterSource, Detection
from ..otlp import (
    MetricPoint,
    Span,
    is_metrics_document,
    is_trace_document,
    parse_metric_points,
    parse_spans,
)

_SERVICE_NAME = "openclaw-localtrace"
_SPAN_NAME_PREFIX = "openclaw-localtrace."

_RUN_SPAN_NAME = "openclaw-localtrace.run"
_LLM_CALL_SPAN_NAME = "openclaw-localtrace.llm.call"
_EVENT_TYPE_BY_SPAN_NAME = {
    "openclaw-localtrace.model.call": "llm_call",
    "openclaw-localtrace.tool.execution": "tool_call",
}

_SESSION_ID_ATTR = "openclaw.sessionId"
_CHANNEL_ATTR = "openclaw.channel"
_CHANNEL_ID_ATTR = "openclaw.channel"

_INPUT_MESSAGES_ATTR = "gen_ai.input.messages"
_OUTPUT_MESSAGES_ATTR = "gen_ai.output.messages"
_TOOL_ARGS_ATTR = "gen_ai.tool.call.arguments"
_TOOL_RESULT_ATTR = "gen_ai.tool.call.result"

_TOKENS_IN_ATTRS = ("gen_ai.usage.input_tokens",)
_TOKENS_IN_CACHE_ATTRS = (
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
)
_TOKENS_OUT_ATTRS = ("gen_ai.usage.output_tokens",)
_COST_USD_ATTR = "gen_ai.usage.cost_usd"
# Always attached by the plugin alongside _COST_USD_ATTR (see its own
# spans.ts) -- this plugin's OWN pricing snapshot's age, unrelated to
# whatever pricing catalog OpenClaw itself uses internally. A missing
# model visibly produces no cost_usd; a provider quietly changing a rate
# produces a confident, plausible-looking dollar figure indistinguishable
# from a correct one -- surfacing this age is what keeps that risk
# visible instead of silent. See docs/openclaw-localtrace.md.
_PRICING_GENERATED_AT_ATTR = "openclaw.pricingTableGeneratedAt"
_PRICING_STALENESS_WARNING_DAYS = 30  # matches the plugin's own Gateway-startup threshold

_MUTATING_ACTION_ATTR = "openclaw.mutatingAction"
_ERROR_ATTR = "openclaw.error"

_TURN_COST_METRIC_NAME = "openclaw.turn.cost.usd"
_TURN_COST_SESSION_ATTR = "openclaw.sessionId"


@dataclass
class ConversionSummary:
    total_spans: int = 0
    kept_spans: int = 0
    skipped_by_kind: dict[str, int] = field(default_factory=dict)

    total_records: int = 0
    records_with_prompt_content: int = 0
    records_with_opaque_content: int = 0

    hash_spec: str = hashing.HASH_SPEC

    llm_call_spans_paired: int = 0
    llm_call_spans_unpaired: int = 0

    sessions_found: int = 0  # distinct real sessionId values seen (0 means captureIdentifiers was off)

    # Per-call cost, straight from the plugin's own bundled-pricing-table
    # estimate on a paired llm.call span (see _llm_event) -- takes
    # priority over the turn-level apportionment below.
    llm_calls_with_direct_cost: int = 0
    # The most recent (latest) pricing-table generatedAt timestamp seen
    # across every direct-cost record -- the plugin's OWN pricing
    # snapshot's age, unrelated to whatever pricing catalog OpenClaw
    # itself uses internally. None means no direct-cost record carried
    # one (an older plugin version, or no direct cost estimates at all).
    pricing_table_generated_at: str | None = None

    # Cost apportionment (see _apportion_cost) -- 0/0 means no
    # openclaw.turn.cost.usd points were captured, or none carried a
    # session id to join against (captureIdentifiers off), not that
    # every llm_call happened to cost nothing. Only ever applied to
    # records that didn't already get a direct per-call estimate above.
    cost_points_found: int = 0
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
        if self.llm_call_spans_unpaired:
            out.append(
                f"{self.llm_call_spans_unpaired} openclaw-localtrace.llm.call span(s) contained "
                "no model.call span at all and were dropped -- their content/token data is not "
                "reflected in any record. See docs/openclaw-localtrace.md."
            )
        if self.total_records == 0:
            out.append("no records produced -- nothing below is meaningful.")
            return out
        out.append(
            f"{self.records_with_opaque_content}/{self.total_records} record(s) "
            f"({self.records_with_opaque_content / self.total_records:.0%}) have no "
            "observable content -- the plugin's own captureContent config was off (the "
            "default), the OpenClaw host's hooks.allowConversationAccess permission "
            "wasn't granted, or this specific event was one this adapter can't get "
            "content from either way. Their content_hash is opaque and cannot match "
            "anything (see metadata.content_basis)."
        )
        if self.sessions_found == 0:
            out.append(
                "task_id is the OTLP trace ID for every record in this corpus -- the "
                "plugin's own captureIdentifiers config was off (the default), so no "
                "span carried a real session id. Repeats spanning more than one turn "
                "are not detectable from this signal. See docs/openclaw-localtrace.md."
            )
        else:
            out.append(
                f"task_id is the real OpenClaw session id for this corpus ({self.sessions_found} "
                "distinct session(s) found) -- redundancy spanning more than one turn is "
                "detectable, unlike every other OpenClaw-derived source in this package."
            )
        if self.llm_calls_with_direct_cost:
            out.append(
                f"cost_usd is a direct per-call ESTIMATE for {self.llm_calls_with_direct_cost} "
                "llm_call record(s) -- computed by the plugin itself from that call's own "
                "token usage against its bundled static pricing snapshot (metadata.cost_basis "
                "= \"estimated_from_bundled_pricing_table\"). A plain provider-published-rate "
                "estimate, not your actual negotiated/discounted billing, and not present for "
                "every llm_call -- only the calls the plugin's own llm.call span pairing "
                "reached (see the note above, if any spans were unpaired)."
            )
            out.append(_pricing_table_age_note(self.pricing_table_generated_at))
        remaining_for_apportionment = self.total_records - self.llm_calls_with_direct_cost
        if self.cost_points_found == 0:
            if remaining_for_apportionment > 0:
                out.append(
                    "No openclaw.turn.cost.usd metric points were found for the remaining "
                    "record(s) -- metrics documents weren't captured, captureIdentifiers was "
                    "off so no point carried a session id to join against, or this source had "
                    "none. See docs/openclaw-localtrace.md."
                )
        else:
            out.append(
                f"cost_usd is ALSO an apportioned turn-level ESTIMATE for "
                f"{self.llm_calls_with_apportioned_cost} further llm_call record(s) that had "
                "no direct per-call estimate above: each turn's openclaw.turn.cost.usd point, "
                "apportioned across that turn's own remaining llm_call events by token share"
                + (
                    f" ({self.llm_calls_apportioned_by_equal_split} of those had no "
                    "token data to share by and were split evenly instead)"
                    if self.llm_calls_apportioned_by_equal_split
                    else ""
                )
                + ". Never an exactly metered per-call figure, and tool_call/tool_result "
                "records never get one -- see docs/openclaw-localtrace.md."
            )
        return out


@dataclass(frozen=True, slots=True)
class _RunWindow:
    task_id: str
    start: int
    end: int
    llm_call_records: list[dict[str, Any]]


def convert_openclaw_localtrace(
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

    records_by_task: dict[str, list[dict[str, Any]]] = {}
    run_windows: list[_RunWindow] = []
    sessions_seen: set[str] = set()

    for trace_id, trace_spans in by_trace.items():
        run_span = next((s for s in trace_spans if s.name == _RUN_SPAN_NAME), None)
        if run_span is not None:
            summary.skipped_by_kind[_RUN_SPAN_NAME] = summary.skipped_by_kind.get(_RUN_SPAN_NAME, 0) + 1

        task_id, session_id = _resolve_task_id(trace_spans, trace_id)
        if session_id is not None:
            sessions_seen.add(session_id)
        workflow = _workflow_of(run_span)

        model_call_spans = sorted(
            (s for s in trace_spans if s.name == "openclaw-localtrace.model.call"),
            key=lambda s: s.start_time_unix_nano,
        )
        llm_call_spans = sorted(
            (s for s in trace_spans if s.name == _LLM_CALL_SPAN_NAME),
            key=lambda s: s.start_time_unix_nano,
        )
        tool_spans = [s for s in trace_spans if s.name == "openclaw-localtrace.tool.execution"]

        enrichment_by_span_id = _pair_llm_call_spans(model_call_spans, llm_call_spans, summary)

        for s in trace_spans:
            if s.name not in _EVENT_TYPE_BY_SPAN_NAME and s.name not in (_RUN_SPAN_NAME, _LLM_CALL_SPAN_NAME):
                summary.skipped_by_kind[s.name] = summary.skipped_by_kind.get(s.name, 0) + 1

        kept = sorted(model_call_spans + tool_spans, key=lambda s: s.start_time_unix_nano)
        summary.kept_spans += len(kept)

        task_records = records_by_task.setdefault(task_id, [])
        run_llm_call_records: list[dict[str, Any]] = []
        run_start = run_span.start_time_unix_nano if run_span is not None else None
        run_end = run_span.end_time_unix_nano if run_span is not None else None

        for span in kept:
            step = len(task_records)
            if span.name == "openclaw-localtrace.model.call":
                record = _llm_event(
                    span, task_id, step, workflow, enrichment_by_span_id.get(span.span_id), summary
                )
                task_records.append(record)
                run_llm_call_records.append(record)
                run_start = span.start_time_unix_nano if run_start is None else min(run_start, span.start_time_unix_nano)
                span_end = span.end_time_unix_nano or span.start_time_unix_nano
                run_end = span_end if run_end is None else max(run_end, span_end)
            else:
                call, result = _tool_events(span, task_id, step, workflow, summary)
                task_records.append(call)
                if result is not None:
                    result["step_index"] = len(task_records)
                    task_records.append(result)
                run_start = span.start_time_unix_nano if run_start is None else min(run_start, span.start_time_unix_nano)
                span_end = span.end_time_unix_nano or span.start_time_unix_nano
                run_end = span_end if run_end is None else max(run_end, span_end)

        if run_llm_call_records and run_start is not None and run_end is not None:
            run_windows.append(
                _RunWindow(task_id=task_id, start=run_start, end=run_end, llm_call_records=run_llm_call_records)
            )

    records: list[dict[str, Any]] = [r for task_records in records_by_task.values() for r in task_records]
    summary.total_records = len(records)
    summary.sessions_found = len(sessions_seen)

    if metrics_docs:
        metric_points: list[MetricPoint] = []
        for document in metrics_docs:
            metric_points.extend(parse_metric_points(document))
        _apportion_cost(run_windows, metric_points, summary)

    return records, summary


def _resolve_task_id(trace_spans: list[Span], trace_id: str) -> tuple[str, str | None]:
    """(task_id, real_session_id_or_None). The real session id -- when
    present -- is the same value on every span in a well-formed capture
    (see module docstring point 1); the first one found is taken as
    authoritative rather than requiring unanimous agreement, since a
    disagreement here would be a plugin bug this adapter can't resolve
    either way.
    """
    for s in trace_spans:
        session_id = s.attributes.get(_SESSION_ID_ATTR)
        if session_id:
            return str(session_id), str(session_id)
    return trace_id, None


def _workflow_of(run_span: Span | None) -> str | None:
    if run_span is None:
        return None
    channel = run_span.attributes.get(_CHANNEL_ATTR) or run_span.attributes.get(_CHANNEL_ID_ATTR)
    return str(channel) if channel else None


def _span_end(span: Span) -> int:
    return span.end_time_unix_nano if span.end_time_unix_nano is not None else span.start_time_unix_nano


def _pair_llm_call_spans(
    model_call_spans: list[Span],
    llm_call_spans: list[Span],
    summary: ConversionSummary,
) -> dict[str, Span]:
    """Pair each openclaw-localtrace.llm.call span with the LAST
    model.call span it temporally contains -- see module docstring point
    2 for why this is a whole-run bracket, not a 1:1 companion, and why
    "last contained call" is the correct target rather than every call
    in the run. Both lists are already sorted by start time by the
    caller.

    llm_call_spans is processed oldest-ending first, and a model.call
    span already claimed by an earlier llm.call is never claimed again --
    relevant only if a run genuinely has more than one llm.call span
    (not observed yet, but not assumed impossible either). A llm.call
    span with no model.call span inside its interval at all (a
    genuinely empty bracket) is left unpaired rather than guessed at.
    """
    if not llm_call_spans:
        return {}
    claimed: set[str] = set()
    pairs: dict[str, Span] = {}
    for llm_span in sorted(llm_call_spans, key=_span_end):
        contained = [
            m
            for m in model_call_spans
            if m.span_id not in claimed and _span_end(m) <= _span_end(llm_span)
        ]
        if not contained:
            summary.llm_call_spans_unpaired += 1
            continue
        last = max(contained, key=_span_end)
        claimed.add(last.span_id)
        pairs[last.span_id] = llm_span
        summary.llm_call_spans_paired += 1
    return pairs


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


def _iso_timestamp(unix_nano: int) -> str:
    return datetime.fromtimestamp(unix_nano / 1e9, tz=timezone.utc).isoformat()


def _parse_pricing_generated_at(value: str) -> datetime | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _pricing_table_age_note(generated_at: str | None) -> str:
    """Always shown (not just past the staleness threshold) alongside a
    direct per-call cost estimate -- see module docstring point 3 and the
    plugin's own README: a stale-but-present price is worse than a
    missing one, so the age needs to be visible every time, not only when
    it happens to be old. This plugin's OWN pricing snapshot's age, never
    to be confused with any pricing OpenClaw itself uses internally.
    """
    if generated_at is None:
        return (
            "This plugin's own pricing-table age could not be determined (an older plugin "
            "version, or no direct-cost records in this corpus) -- treat cost_usd estimates "
            "with that in mind."
        )
    parsed = _parse_pricing_generated_at(generated_at)
    if parsed is None:
        return f"This plugin's own pricing table reports an unparseable generatedAt ({generated_at!r})."
    age_days = (datetime.now(timezone.utc) - parsed).days
    note = (
        f"This plugin's OWN pricing table (not OpenClaw's built-in pricing) was generated "
        f"{generated_at} ({age_days} day(s) ago)."
    )
    if age_days > _PRICING_STALENESS_WARNING_DAYS:
        note += (
            f" That's over {_PRICING_STALENESS_WARNING_DAYS} days -- provider rates may have "
            "changed since then; refresh with: npx openclaw-localtrace-update-pricing (then "
            "restart the Gateway and re-capture)."
        )
    return note


def _opaque_hash(span: Span) -> tuple[str, int]:
    """A content_hash derived from the span's own id, not its content --
    unique per span by construction. Same idiom as sources.openclaw's
    helper of the same name.
    """
    return hashing.content_hash(span.span_id, structured=False)


def _base_metadata(
    span: Span,
    masked_spans: int,
    content_basis: str,
    task_id_source: str,
    similarity_fingerprint: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "hash_spec": hashing.HASH_SPEC,
        "masked_spans": masked_spans,
        "otlp_span_id": span.span_id,
        "otlp_trace_id": span.trace_id,
        "task_id_source": task_id_source,
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
    workflow: str | None,
    llm_call_span: Span | None,
    summary: ConversionSummary,
) -> dict[str, Any]:
    task_id_source = "conversation_id" if span.attributes.get(_SESSION_ID_ATTR) else "trace_id_fallback"

    input_raw = llm_call_span.attributes.get(_INPUT_MESSAGES_ATTR) if llm_call_span else None
    if input_raw is None:
        summary.records_with_opaque_content += 1
        digest, masks = _opaque_hash(span)
        content_basis, fingerprint = "opaque", None
    else:
        digest, masks = hashing.content_hash(input_raw, structured=True)
        fingerprint = hashing.similarity_fingerprint(input_raw, structured=True)
        summary.records_with_prompt_content += 1
        content_basis = "prompt"

    response_hash = None
    output_raw = llm_call_span.attributes.get(_OUTPUT_MESSAGES_ATTR) if llm_call_span else None
    if output_raw is not None:
        response_hash, _ = hashing.content_hash(output_raw, structured=True)

    tokens_in = tokens_out = None
    cost_usd = None
    if llm_call_span is not None:
        tokens_in_raw = _first_present(llm_call_span.attributes, _TOKENS_IN_ATTRS)
        if tokens_in_raw is not None:
            tokens_in = int(tokens_in_raw) + _sum_present(llm_call_span.attributes, _TOKENS_IN_CACHE_ATTRS)
        tokens_out_raw = _first_present(llm_call_span.attributes, _TOKENS_OUT_ATTRS)
        tokens_out = int(tokens_out_raw) if tokens_out_raw is not None else None
        cost_usd_raw = llm_call_span.attributes.get(_COST_USD_ATTR)
        if cost_usd_raw is not None:
            cost_usd = float(cost_usd_raw)

    metadata = _base_metadata(span, masks, content_basis, task_id_source, fingerprint)
    if response_hash is not None:
        metadata["response_hash"] = response_hash
    if cost_usd is not None:
        # A real per-call estimate, from the plugin's own bundled pricing
        # snapshot -- takes priority over _apportion_cost's turn-level
        # apportionment below (see its own docstring: it now skips any
        # record that already has one). Still an estimate against
        # provider-published rates, not a certified per-call billed
        # amount -- see docs/openclaw-localtrace.md.
        metadata["cost_basis"] = "estimated_from_bundled_pricing_table"
        summary.llm_calls_with_direct_cost += 1
        generated_at = llm_call_span.attributes.get(_PRICING_GENERATED_AT_ATTR) if llm_call_span else None
        if generated_at:
            metadata["pricing_table_generated_at"] = str(generated_at)
            # Track the latest (not just the first) -- ISO-8601 UTC
            # timestamps compare correctly as plain strings, and a longer
            # capture could span more than one pricing-table refresh.
            if summary.pricing_table_generated_at is None or str(generated_at) > summary.pricing_table_generated_at:
                summary.pricing_table_generated_at = str(generated_at)

    model = span.attributes.get("openclaw.model")
    outcome = "error" if span.attributes.get("openclaw.errorCategory") else ("ok" if span.end_time_unix_nano else None)

    return {
        "task_id": task_id,
        "step_index": step,
        "event_type": "llm_call",
        "name": model or span.name,
        "content_hash": digest,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "outcome": outcome,
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        "cost_usd": cost_usd,  # else filled in by _apportion_cost, if metrics were captured
        "model": str(model) if model else None,
        "parent_id": None,  # real topology is flat under the run wrapper -- see module docstring point 4
        "workflow": workflow,
        "metadata": metadata,
    }


def _tool_events(
    span: Span,
    task_id: str,
    step: int,
    workflow: str | None,
    summary: ConversionSummary,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    task_id_source = "conversation_id" if span.attributes.get(_SESSION_ID_ATTR) else "trace_id_fallback"
    tool_name = span.attributes.get("openclaw.toolName") or span.name

    raw_args = span.attributes.get(_TOOL_ARGS_ATTR)
    if raw_args is None:
        summary.records_with_opaque_content += 1
        call_hash, call_masks = _opaque_hash(span)
        call_basis, call_fingerprint = "opaque", None
    else:
        call_hash, call_masks = hashing.content_hash(raw_args, structured=True)
        call_fingerprint = hashing.similarity_fingerprint(raw_args, structured=True)
        summary.records_with_prompt_content += 1
        call_basis = "prompt"

    error = span.attributes.get(_ERROR_ATTR)
    has_ended = span.end_time_unix_nano is not None

    metadata = _base_metadata(span, call_masks, call_basis, task_id_source, call_fingerprint)
    mutating = span.attributes.get(_MUTATING_ACTION_ATTR)
    if isinstance(mutating, bool):
        # The single most valuable capability unlock this source has
        # over every other OpenClaw-derived one: a real, host-adjacent
        # write/mutation classification (see the plugin's own
        # mutatingToolNames config) instead of always UNKNOWN.
        metadata["write"] = mutating

    call = {
        "task_id": task_id,
        "step_index": step,
        "event_type": "tool_call",
        "name": str(tool_name),
        "content_hash": call_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": "error" if error else None,
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": None,
        "workflow": workflow,
        "metadata": metadata,
    }

    raw_result = span.attributes.get(_TOOL_RESULT_ATTR)
    if raw_result is None:
        # No result attribute at all -- captureContent was off, or the
        # call errored before producing output. No tool_result event,
        # same discipline every other source in this package follows: an
        # absent event reads as UNKNOWN downstream, never a fabricated
        # hash that could look like a real match or mismatch.
        return call, None

    result_hash, result_masks = hashing.content_hash(raw_result, structured=True)
    summary.records_with_prompt_content += 1

    result = {
        "task_id": task_id,
        "step_index": None,  # filled in by convert_openclaw_localtrace() once the call's step is known
        "event_type": "tool_result",
        "name": str(tool_name),
        "content_hash": result_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": "error" if error else ("ok" if has_ended else None),
        "timestamp": _iso_timestamp(span.end_time_unix_nano or span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": None,
        "workflow": workflow,
        "metadata": _base_metadata(span, result_masks, "prompt", task_id_source),
    }
    return call, result


def _apportion_cost(
    run_windows: list[_RunWindow],
    metric_points: list[MetricPoint],
    summary: ConversionSummary,
) -> None:
    """Split each openclaw.turn.cost.usd point across the one run's own
    llm_call records it belongs to -- by token share when those records
    have token data, evenly otherwise. See module docstring point 3 for
    why this is simpler than sources.openclaw's restart-generation
    windowing: there is no counter to reset here, just one independent
    point per turn.

    A point is matched to the run, for the same session, whose own
    [start, end) window ends most recently at or before the point's own
    timestamp -- the point is written shortly after that run's reply is
    sent, so this is the run it almost certainly describes. A point with
    no session-identifying attribute (captureIdentifiers was off) or no
    matching run for its session is skipped, not guessed at.

    Records that already have a direct per-call estimate (from the
    plugin's own bundled pricing table -- see _llm_event) are excluded
    entirely from both the token-share denominator and the apportionment
    itself: a direct per-call figure is strictly more precise than a
    turn-level split, and double-applying both to the same record would
    overstate its cost.
    """
    points = [
        p
        for p in metric_points
        if p.name == _TURN_COST_METRIC_NAME and p.kind == "gauge" and p.value is not None
    ]
    summary.cost_points_found = len(points)
    if not points:
        return

    windows_by_task: dict[str, list[_RunWindow]] = {}
    for window in run_windows:
        windows_by_task.setdefault(window.task_id, []).append(window)
    for windows in windows_by_task.values():
        windows.sort(key=lambda w: w.end)

    contributions: dict[int, float] = {}
    apportioned_records: dict[int, dict[str, Any]] = {}
    equal_split_ids: set[int] = set()

    for point in points:
        session_id = point.attributes.get(_TURN_COST_SESSION_ATTR)
        if not session_id:
            continue
        candidates = windows_by_task.get(str(session_id))
        if not candidates:
            continue
        matched = None
        for window in candidates:
            if window.end <= point.time_unix_nano:
                matched = window
            else:
                break
        if matched is None:
            continue

        eligible = [r for r in matched.llm_call_records if r["cost_usd"] is None]
        if not eligible:
            continue
        total_tokens = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in eligible)
        for record in eligible:
            record_id = id(record)
            apportioned_records[record_id] = record
            if total_tokens > 0:
                record_tokens = (record["tokens_in"] or 0) + (record["tokens_out"] or 0)
                share = record_tokens / total_tokens
            else:
                share = 1 / len(eligible)
                equal_split_ids.add(record_id)
            contributions[record_id] = contributions.get(record_id, 0.0) + point.value * share

    for record_id, total in contributions.items():
        record = apportioned_records[record_id]
        record["cost_usd"] = round(total, 6)
        record["metadata"]["cost_basis"] = (
            "apportioned_from_metrics_equal_split"
            if record_id in equal_split_ids
            else "apportioned_from_metrics_by_tokens"
        )
        summary.llm_calls_with_apportioned_cost += 1
        if record_id in equal_split_ids:
            summary.llm_calls_apportioned_by_equal_split += 1


class OpenClawLocaltraceSource(AdapterSource):
    name = "openclaw-localtrace"

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if not is_trace_document(doc):
                continue
            for span in parse_spans(doc):
                if span.name.startswith(_SPAN_NAME_PREFIX):
                    return Detection("openclaw-localtrace", f"span name {span.name!r}")
                if span.resource_attributes.get("service.name") == _SERVICE_NAME:
                    return Detection(
                        "openclaw-localtrace", f"resource service.name={_SERVICE_NAME!r}"
                    )
        return None

    def convert(self, documents: list[dict[str, Any]]):
        return convert_openclaw_localtrace(documents)
