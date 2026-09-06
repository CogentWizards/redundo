"""OpenInference spans -> redundo.analyzer's Event schema (as plain dicts;
see docs/openinference.md, not a shared class -- the adapter has no runtime dependency
on the analyzer).

Three rules that aren't obvious from the code and are easy to silently
violate while extending this:

1. task_id: gen_ai.conversation.id when a trace's spans agree on one
   value, otherwise the trace ID. Never anything else. A synthesized
   grouping key produces confidently wrong repeat counts instead of a
   visible gap -- see docs/openinference.md.
2. Only openinference.span.kind == LLM or TOOL become Events. Every other
   kind (CHAIN, AGENT, RETRIEVER, ...) doesn't map cleanly onto
   llm_call/tool_call/tool_result and is skipped, counted, and reported --
   not guessed at.
3. Lineage (parent_id) walks the real OTLP parent_span_id chain, skipping
   over skipped-kind spans to find the nearest ancestor that was actually
   converted. This is real branch structure, not the analyzer's linear
   fallback -- that fallback only kicks in for spans with no kept ancestor
   at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import hashing
from ..base import AdapterSource, Detection
from ..otlp import Span, is_trace_document, parse_spans

SUPPORTED_KINDS = frozenset({"LLM", "TOOL"})
_WORKFLOW_KINDS = frozenset({"AGENT", "CHAIN"})

_KIND_ATTR = "openinference.span.kind"
_CONVERSATION_ID_ATTR = "gen_ai.conversation.id"
_INPUT_ATTR = "input.value"
_OUTPUT_ATTR = "output.value"
_INPUT_MIME_ATTR = "input.mime_type"
_OUTPUT_MIME_ATTR = "output.mime_type"
_TOOL_NAME_ATTRS = ("tool.name",)
_MODEL_ATTRS = ("llm.model_name", "gen_ai.request.model")
_TOKENS_IN_ATTRS = ("llm.token_count.prompt", "gen_ai.usage.input_tokens")
_TOKENS_OUT_ATTRS = ("llm.token_count.completion", "gen_ai.usage.output_tokens")
# Cost is not a stable OpenInference/gen_ai convention as of this writing;
# these are best-effort and will usually be absent. That's fine -- the
# analyzer falls back to token counts when cost_usd is None.
_COST_ATTRS = ("llm.cost.total", "cost.total_usd")


@dataclass
class ConversionSummary:
    total_spans: int = 0
    kept_spans: int = 0
    skipped_by_kind: dict[str, int] = field(default_factory=dict)
    skipped_missing_content: int = 0

    traces_total: int = 0
    traces_with_conversation_id: int = 0
    traces_fallback_to_trace_id: int = 0
    traces_ambiguous_conversation_id: int = 0

    total_records: int = 0
    records_with_any_mask: int = 0
    masked_span_total: int = 0

    hash_spec: str = hashing.HASH_SPEC

    @property
    def mask_fraction(self) -> float:
        return self.records_with_any_mask / self.total_records if self.total_records else 0.0

    def notes(self) -> list[str]:
        """Human-readable lines meant to go straight into a report or log --
        the honest-degradation messages this adapter exists to produce.
        """
        out: list[str] = []
        if self.traces_fallback_to_trace_id:
            out.append(
                f"no gen_ai.conversation.id found for {self.traces_fallback_to_trace_id} "
                "trace(s); grouped by trace ID instead -- cross-trace rework not detected "
                "for these."
            )
        if self.traces_ambiguous_conversation_id:
            out.append(
                f"{self.traces_ambiguous_conversation_id} trace(s) had conflicting "
                "gen_ai.conversation.id values across their own spans; fell back to "
                "trace ID for those rather than guessing which value was right."
            )
        if self.skipped_by_kind:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped_by_kind.items()))
            out.append(
                f"skipped {sum(self.skipped_by_kind.values())} span(s) with a "
                f"{_KIND_ATTR} this adapter doesn't map to an event type ({detail})."
            )
        if self.skipped_missing_content:
            out.append(
                f"skipped {self.skipped_missing_content} LLM/TOOL span(s) with no "
                f"{_INPUT_ATTR} to hash -- no content, no candidate for repeat detection."
            )
        if self.total_records == 0:
            out.append("no records produced -- nothing below is meaningful.")
        elif self.records_with_any_mask == 0:
            out.append(
                "no content was masked. If nothing in this trace varies "
                "(dates, UUIDs, call IDs), that's consistent with zero masking being "
                "correct. If it does vary, the masking patterns in hashing.py may need "
                "extending -- zero repeats found downstream would otherwise look "
                "identical to zero repeats existing."
            )
        else:
            out.append(
                f"{self.records_with_any_mask}/{self.total_records} record(s) "
                f"({self.mask_fraction:.0%}) had at least one volatile span masked."
            )
        return out


def convert_openinference(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ConversionSummary]:
    """documents: one or more parsed OTLP traces JSON export documents.
    A source's own exporter typically flushes on an interval, producing
    many small batch files per session rather than one large export --
    accepting a list (rather than a single document, as earlier versions
    of this function did) is what lets a whole captured directory be
    converted in one call.
    """
    spans: list[Span] = []
    for document in documents:
        spans.extend(parse_spans(document))
    summary = ConversionSummary(total_spans=len(spans))

    by_trace: dict[str, list[Span]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)

    records: list[dict[str, Any]] = []

    for trace_id, trace_spans in by_trace.items():
        summary.traces_total += 1
        task_id, used_conversation_id, ambiguous = _resolve_task_id(trace_id, trace_spans)
        if used_conversation_id:
            summary.traces_with_conversation_id += 1
        else:
            summary.traces_fallback_to_trace_id += 1
        if ambiguous:
            summary.traces_ambiguous_conversation_id += 1

        span_by_id = {s.span_id: s for s in trace_spans}
        kept = [s for s in trace_spans if _kind_of(s) in SUPPORTED_KINDS]
        for s in trace_spans:
            kind = _kind_of(s)
            if kind not in SUPPORTED_KINDS:
                key = kind or "(missing)"
                summary.skipped_by_kind[key] = summary.skipped_by_kind.get(key, 0) + 1
        kept.sort(key=lambda s: s.start_time_unix_nano)
        summary.kept_spans += len(kept)

        step = 0
        last_step_of_span: dict[str, int] = {}
        # Real OTLP span parenting reflects call-stack nesting, not
        # turn-to-turn execution order. Some instrumentation (observed from
        # hermes-otel on a single agentic loop) puts every turn's LLM- and
        # tool-call spans directly under one flat root span, with no
        # deeper nesting at all -- so raw parent_span_id alone can't tell
        # a sequential repeat from independent parallel fan-out; both look
        # like plain siblings of the same ancestor. redundo.analyzer's
        # lineage model deliberately never chains siblings to each other
        # (real parallel branches making the same call is normal fan-out,
        # not waste), so a flat topology like this would make it silently
        # miss every sequential repeat in the loop -- not misclassify it,
        # just never generate a candidate pair for it at all.
        #
        # The adapter has genuine interval data the schema does not (each
        # span's own start/end nanoseconds; Event.timestamp is a single
        # point). Use it: chain a span to the nearest earlier sibling
        # under the same real ancestor when their intervals don't
        # overlap -- strong evidence of "next sequential step," not
        # "concurrent branch." Overlapping siblings stay attached directly
        # to the real ancestor, preserving true parallel fan-out.
        #
        # The tail only ever extends forward (see _extend_chain_tail): a
        # short span nested entirely inside a longer-running one (observed
        # in practice -- a `terminal` sub-call whose interval sits inside
        # its parent `execute_code` call's own span) must not regress the
        # watermark backward. Doing so would make the *next* real
        # sequential step compare against the nested span's early end
        # instead of the still-open outer one, misreading it as
        # "overlapping" too and fragmenting one continuous thread into
        # disconnected pieces that each fork back to the shared ancestor
        # -- silently breaking candidate-pair detection between anything
        # before and after the nested span, however far apart.
        chain_tail: dict[str, tuple[int, int, int]] = {}  # ancestor span_id -> (start, end, step)

        for span in kept:
            kind = _kind_of(span)
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

            if kind == "LLM":
                record = _llm_event(
                    span, task_id, used_conversation_id, step, parent_step, span_by_id, summary
                )
                if record is None:
                    summary.skipped_missing_content += 1
                    continue
                records.append(record)
                last_step_of_span[span.span_id] = step
                _extend_chain_tail(chain_tail, ancestor_id, span.start_time_unix_nano, span_end, step)
                step += 1

            else:  # TOOL
                call, result = _tool_events(
                    span, task_id, used_conversation_id, step, parent_step, span_by_id, summary
                )
                if call is None:
                    summary.skipped_missing_content += 1
                    continue
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


def _resolve_task_id(trace_id: str, trace_spans: list[Span]) -> tuple[str, bool, bool]:
    """(task_id, used_conversation_id, ambiguous). Scans every span in the
    trace for gen_ai.conversation.id -- OTel context propagation means it's
    common for only the root span, or only some spans, to carry it.
    """
    values = {
        span.attributes[_CONVERSATION_ID_ATTR]
        for span in trace_spans
        if span.attributes.get(_CONVERSATION_ID_ATTR)
    }
    if len(values) == 1:
        return next(iter(values)), True, False
    if len(values) > 1:
        return trace_id, False, True
    return trace_id, False, False


def _kind_of(span: Span) -> str | None:
    value = span.attributes.get(_KIND_ATTR)
    return str(value).upper() if value else None


def _resolve_kept_ancestor_span_id(
    span: Span, span_by_id: dict[str, Span], last_step_of_span: dict[str, int]
) -> str | None:
    """Walk the real parent_span_id chain to the nearest ancestor span that
    was itself kept (converted into a record), skipping unsupported kinds
    (e.g. AGENT/CHAIN wrapper spans). Returns that ancestor's own span_id
    -- the caller resolves it to a step_index, since the ancestor may have
    since been superseded as the sibling-chaining tail (see convert()).
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
    """Advance the sibling-chaining watermark for `ancestor_id` to this
    span, but only if it doesn't regress the end-time watermark backward.
    A span whose own interval is nested inside a still-later-ending sibling
    (already forked off as non-sequential -- see convert()) must not become
    the new comparison point for whatever comes next; the watermark stays
    on the later-ending sibling until something genuinely starts after it.
    """
    if ancestor_id is None:
        return
    tail = chain_tail.get(ancestor_id)
    if tail is None or end >= tail[1]:
        chain_tail[ancestor_id] = (start, end, step)


def _workflow_of(span: Span, span_by_id: dict[str, Span]) -> str | None:
    """Best-effort segmentation label: the nearest AGENT/CHAIN ancestor's
    span name. Unlike task_id, this has no "never guess" constraint --
    workflow is inherently an approximate label, so a documented heuristic
    is fine. None (not a guess) when no such ancestor exists.
    """
    current_id = span.parent_span_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        parent = span_by_id.get(current_id)
        if parent is None:
            return None
        if _kind_of(parent) in _WORKFLOW_KINDS:
            return parent.name
        current_id = parent.parent_span_id
    return None


def _first_present(attributes: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if attributes.get(key) is not None:
            return attributes[key]
    return None


def _is_structured(attributes: dict[str, Any], mime_key: str, raw_value: Any) -> bool:
    mime = attributes.get(mime_key)
    if isinstance(mime, str):
        return "json" in mime.lower()
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        return stripped.startswith("{") or stripped.startswith("[")
    return not isinstance(raw_value, str)


def _outcome(status_code: int | None) -> str | None:
    if status_code == 2:
        return "error"
    if status_code == 1:
        return "ok"
    return None


def _iso_timestamp(unix_nano: int) -> str:
    return datetime.fromtimestamp(unix_nano / 1e9, tz=timezone.utc).isoformat()


def _base_metadata(span: Span, masked_spans: int, used_conversation_id: bool) -> dict[str, Any]:
    return {
        "hash_spec": hashing.HASH_SPEC,
        "masked_spans": masked_spans,
        "otlp_span_id": span.span_id,
        "otlp_trace_id": span.trace_id,
        # Lets a consuming report distinguish "grouped by a real conversation
        # id" from "grouped by trace id because conversation.id was absent"
        # -- the latter means cross-trace rework isn't detected for this
        # event, which matters for how much a reader should trust the
        # grouping before reading percentages off it.
        "task_id_source": "conversation_id" if used_conversation_id else "trace_id_fallback",
    }


def _llm_event(
    span: Span,
    task_id: str,
    used_conversation_id: bool,
    step: int,
    parent_step: int | None,
    span_by_id: dict[str, Span],
    summary: ConversionSummary,
) -> dict[str, Any] | None:
    raw_input = _first_present(span.attributes, (_INPUT_ATTR,))
    if raw_input is None:
        return None

    structured = _is_structured(span.attributes, _INPUT_MIME_ATTR, raw_input)
    digest, mask_count = hashing.content_hash(raw_input, structured=structured)
    _record_mask_stats(summary, mask_count)

    model = _first_present(span.attributes, _MODEL_ATTRS)
    tokens_in = _first_present(span.attributes, _TOKENS_IN_ATTRS)
    tokens_out = _first_present(span.attributes, _TOKENS_OUT_ATTRS)
    cost = _first_present(span.attributes, _COST_ATTRS)

    return {
        "task_id": task_id,
        "step_index": step,
        "event_type": "llm_call",
        "name": model or span.name,
        "content_hash": digest,
        "tokens_in": int(tokens_in) if tokens_in is not None else None,
        "tokens_out": int(tokens_out) if tokens_out is not None else None,
        "outcome": _outcome(span.status_code),
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        "cost_usd": float(cost) if cost is not None else None,
        "model": model,
        "parent_id": parent_step,
        "workflow": _workflow_of(span, span_by_id),
        "metadata": _base_metadata(span, mask_count, used_conversation_id),
    }


def _tool_events(
    span: Span,
    task_id: str,
    used_conversation_id: bool,
    step: int,
    parent_step: int | None,
    span_by_id: dict[str, Span],
    summary: ConversionSummary,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw_input = _first_present(span.attributes, (_INPUT_ATTR,))
    if raw_input is None:
        return None, None

    structured_in = _is_structured(span.attributes, _INPUT_MIME_ATTR, raw_input)
    call_hash, call_masks = hashing.content_hash(raw_input, structured=structured_in)
    _record_mask_stats(summary, call_masks)

    tool_name = _first_present(span.attributes, _TOOL_NAME_ATTRS) or span.name
    workflow = _workflow_of(span, span_by_id)

    call = {
        "task_id": task_id,
        "step_index": step,
        "event_type": "tool_call",
        "name": tool_name,
        "content_hash": call_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": None,
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": parent_step,
        "workflow": workflow,
        "metadata": _base_metadata(span, call_masks, used_conversation_id),
    }

    raw_output = _first_present(span.attributes, (_OUTPUT_ATTR,))
    if raw_output is None:
        return call, None

    structured_out = _is_structured(span.attributes, _OUTPUT_MIME_ATTR, raw_output)
    result_hash, result_masks = hashing.content_hash(raw_output, structured=structured_out)
    _record_mask_stats(summary, result_masks)

    result = {
        "task_id": task_id,
        "step_index": None,  # filled in by convert() once the call's step is known
        "event_type": "tool_result",
        "name": tool_name,
        "content_hash": result_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": _outcome(span.status_code),
        "timestamp": _iso_timestamp(span.end_time_unix_nano or span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": None,  # filled in by convert()
        "workflow": workflow,
        "metadata": _base_metadata(span, result_masks, used_conversation_id),
    }
    return call, result


def _record_mask_stats(summary: ConversionSummary, mask_count: int) -> None:
    summary.masked_span_total += mask_count
    if mask_count:
        summary.records_with_any_mask += 1


class OpenInferenceSource(AdapterSource):
    name = "openinference"

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if not is_trace_document(doc):
                continue
            for span in parse_spans(doc):
                if _KIND_ATTR in span.attributes:
                    return Detection("openinference", f"span attribute {_KIND_ATTR!r}")
        return None

    def convert(self, documents: list[dict[str, Any]]):
        trace_docs = [d for d in documents if is_trace_document(d)]
        return convert_openinference(trace_docs)
