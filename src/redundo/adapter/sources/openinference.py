"""OpenInference spans -> redundo.analyzer's Event schema (as plain dicts;
see docs/openinference.md, not a shared class -- the adapter has no runtime dependency
on the analyzer).

Four rules that aren't obvious from the code and are easy to silently
violate while extending this:

1. task_id is resolved **per span**, not once for the whole trace:
   gen_ai.conversation.id (or session.id, the same concept under
   OpenInference's own separate attribute name, confirmed real for
   Google ADK, which sets it directly to its own session id) if the
   span carries one itself, else the nearest ancestor's (walking the
   real parent_span_id chain), else that span's own trace ID. Never
   anything else. A synthesized grouping key produces confidently wrong
   repeat counts instead of a visible gap -- see docs/openinference.md.

   This used to be resolved once per trace, requiring every span in a
   trace to agree on a single value or falling the whole trace back to
   its trace ID. That was wrong for a real, confirmed shape: hermes-otel
   puts a parent conversation and each of its `delegate_task`-spawned
   subagents in *one* OTel trace, each with its own distinct, genuinely
   real session id stamped directly on its own spans -- not a data
   quality problem to distrust, just multiple real tasks sharing one
   trace. Per-span resolution handles this for free: each span's own
   stated identity is trusted directly, never overridden by what an
   unrelated span elsewhere in the same trace happens to say about
   itself. There is no remaining "conflicting values" case to guess at:
   once every span answers for itself, two different spans reporting two
   different real ids is simply two different real tasks, not ambiguity.
2. Only openinference.span.kind == LLM or TOOL become Events. Every other
   kind (CHAIN, AGENT, RETRIEVER, ...) doesn't map cleanly onto
   llm_call/tool_call/tool_result and is skipped, counted, and reported --
   not guessed at.
3. Lineage (parent_id) walks the real OTLP parent_span_id chain, skipping
   over skipped-kind spans to find the nearest ancestor that was actually
   converted. This is real branch structure, not the analyzer's linear
   fallback -- that fallback only kicks in for spans with no kept ancestor
   at all. Critically, this lineage-chaining bookkeeping (step_index,
   the sibling-chaining tail) is scoped **per resolved task_id, not per
   trace_id**: a task_id can legitimately span more than one physical
   trace (a source that gives one CLI invocation its own trace per turn
   but a stable session id across turns, confirmed for Hermes), and a
   single trace can legitimately contain more than one task_id (the
   subagent-delegation shape above). Grouping by resolved task_id first,
   then running one continuous step counter per group, is what keeps
   step_index unique within a task_id no matter how many physical traces
   or subagents it's assembled from -- see convert_openinference()'s own
   two-phase structure.
4. metadata.parent_task_id (a real, source-confirmed delegation link,
   e.g. hermes-otel's hermes.subagent.parent_session_id) is *also*
   resolved via an ancestor walk, not just the span's own attributes:
   confirmed against a real capture, the attribute lives on the
   subagent's own wrapping AGENT-kind span (never converted into an
   Event itself), not on the LLM/TOOL spans nested inside it that
   actually need it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import hashing, pricing
from ..base import AdapterSource, Detection
from ..model_inference import fill_missing_tool_model
from ..otlp import Span, is_trace_document, parse_spans

SUPPORTED_KINDS = frozenset({"LLM", "TOOL"})
_WORKFLOW_KINDS = frozenset({"AGENT", "CHAIN"})

# workflow: no AGENT/CHAIN ancestor exists for this event at all -- not a
# guess, a real fact (this ran at the top level of the task, not inside a
# delegated sub-workflow). The same literal every adapter in this package
# uses for the equivalent case, so a reader sees one consistent label
# across sources.
_NO_WORKFLOW_LABEL = "main"
# The real, human-chosen agent name -- confirmed set by both
# openinference-instrumentation-openai-agents (Agent(name=...), where it
# happens to equal the span's own name too) and
# openinference-instrumentation-google-adk (LlmAgent's own .name, where
# it does NOT equal the span's own name: ADK's AGENT-kind span is named
# "agent_run [<name>]", decorated, while this attribute carries the bare
# name). Checked first for that reason -- relying on the span's own name
# alone silently surfaces ADK's decorated form.
_AGENT_NAME_ATTR = "agent.name"

_KIND_ATTR = "openinference.span.kind"
# gen_ai.conversation.id is the primary attribute; session.id is
# OpenInference's own separate convention for the identical concept,
# confirmed real for Google ADK (openinference-instrumentation-google-adk
# maps ADK's own session id onto session.id, never onto
# gen_ai.conversation.id at all), not a guessed synonym.
_CONVERSATION_ID_ATTRS = ("gen_ai.conversation.id", "session.id")
# A real, source-confirmed link to another task this one was delegated
# from, never inferred from timing or content similarity. Confirmed
# real and already on the wire for hermes-otel's subagent spans; absent
# for every other source until one exposes an equivalent.
_PARENT_TASK_ID_ATTRS = ("hermes.subagent.parent_session_id",)
_INPUT_ATTR = "input.value"
_OUTPUT_ATTR = "output.value"
_INPUT_MIME_ATTR = "input.mime_type"
_OUTPUT_MIME_ATTR = "output.mime_type"
_TOOL_NAME_ATTRS = ("tool.name",)
_MODEL_ATTRS = ("llm.model_name", "gen_ai.request.model")
_TOKENS_IN_ATTRS = ("llm.token_count.prompt", "gen_ai.usage.input_tokens")
_TOKENS_OUT_ATTRS = ("llm.token_count.completion", "gen_ai.usage.output_tokens")
_CACHE_READ_TOKEN_ATTRS = (
    "llm.token_count.prompt_details.cache_read",
    "gen_ai.usage.cache_read_input_tokens",
    "gen_ai.usage.cache_read.input_tokens",
)
_CACHE_WRITE_TOKEN_ATTRS = (
    "llm.token_count.prompt_details.cache_write",
    "gen_ai.usage.cache_creation_input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
)
# Cost is not a stable OpenInference/gen_ai convention as of this writing;
# these are best-effort and will usually be absent. When absent, this
# adapter estimates cost_usd itself from token counts against a bundled
# pricing table (see pricing.py) rather than leaving it None whenever
# both a recognized model and real token counts are present.
_COST_ATTRS = ("llm.cost.total", "cost.total_usd")


@dataclass
class ConversionSummary:
    total_spans: int = 0
    kept_spans: int = 0
    skipped_by_kind: dict[str, int] = field(default_factory=dict)
    skipped_missing_content: int = 0

    traces_total: int = 0
    # Resolved per span, not per trace -- see module docstring point 1.
    # A single trace can contain a real mix of both (a parent
    # conversation plus subagents that each carry their own id), so these
    # count spans, not traces.
    spans_with_conversation_id: int = 0
    spans_fallback_to_trace_id: int = 0

    total_records: int = 0
    records_with_any_mask: int = 0
    masked_span_total: int = 0

    hash_spec: str = hashing.HASH_SPEC
    # Set whenever at least one record's cost_usd was estimated from the
    # bundled/override pricing table (see pricing.py) rather than read
    # directly off the trace. None means no estimate was ever used,
    # nothing to report on the table's age.
    pricing_table_generated_at: str | None = None
    # Count of LLM-kind child spans whose token counts were borrowed into
    # their parent LLM span's record -- see _find_llm_token_donor_child.
    # Some exporters (hermes-otel, confirmed against a real capture) split
    # content and token usage across two spans instead of one.
    merged_token_child_spans: int = 0

    @property
    def mask_fraction(self) -> float:
        return self.records_with_any_mask / self.total_records if self.total_records else 0.0

    def notes(self) -> list[str]:
        """Human-readable lines meant to go straight into a report or log --
        the honest-degradation messages this adapter exists to produce.
        """
        out: list[str] = []
        if self.spans_fallback_to_trace_id:
            out.append(
                f"no gen_ai.conversation.id found for {self.spans_fallback_to_trace_id} "
                "span(s) (on the span itself or any ancestor); grouped by trace ID "
                "instead -- cross-trace rework not detected for these."
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
        if self.merged_token_child_spans:
            out.append(
                f"merged token counts from {self.merged_token_child_spans} child "
                "span(s) into their parent llm_call record; some exporters split "
                "content and token usage across two spans instead of one."
            )
        pricing_note = pricing.pricing_staleness_note(self.pricing_table_generated_at)
        if pricing_note:
            out.append(f"cost_usd was estimated for one or more records; {pricing_note}.")
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
    *,
    pricing_context: pricing.PricingContext | None = None,
) -> tuple[list[dict[str, Any]], ConversionSummary]:
    """documents: one or more parsed OTLP traces JSON export documents.
    A source's own exporter typically flushes on an interval, producing
    many small batch files per session rather than one large export --
    accepting a list (rather than a single document, as earlier versions
    of this function did) is what lets a whole captured directory be
    converted in one call.

    pricing_context: injectable for tests; defaults to
    pricing.load_pricing_context() (the bundled table, overlaid by
    ~/.redundo/pricing-table.json if present) when not given.
    """
    if pricing_context is None:
        pricing_context = pricing.load_pricing_context()

    spans: list[Span] = []
    for document in documents:
        spans.extend(parse_spans(document))
    summary = ConversionSummary(total_spans=len(spans))

    by_trace: dict[str, list[Span]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)

    # Phase 1 (per trace): resolve each span's own task_id/parent_task_id
    # (own attribute, else nearest ancestor's -- a real task boundary, e.g.
    # a Hermes subagent, can start partway through a trace) and build the
    # ancestor-lookup structures every later step needs. span_by_id and
    # children_by_parent are kept global (spanning every trace) rather
    # than rebuilt per trace: parent_span_id references are only ever
    # meaningful within the trace that produced them, so a lookup never
    # accidentally crosses a trace boundary just because the dict is
    # shared -- sharing it only saves rebuilding it per phase.
    span_by_id: dict[str, Span] = {}
    children_by_parent: dict[str, list[Span]] = {}
    span_task_id: dict[str, str] = {}
    span_used_conversation_id: dict[str, bool] = {}
    span_parent_task_id: dict[str, str | None] = {}

    for trace_id, trace_spans in by_trace.items():
        summary.traces_total += 1
        for s in trace_spans:
            span_by_id[s.span_id] = s
            if s.parent_span_id:
                children_by_parent.setdefault(s.parent_span_id, []).append(s)

        for s in trace_spans:
            kind = _kind_of(s)
            if kind not in SUPPORTED_KINDS:
                key = kind or "(missing)"
                summary.skipped_by_kind[key] = summary.skipped_by_kind.get(key, 0) + 1

        # A second pass, after span_by_id/children_by_parent above are
        # fully populated for this trace, since the ancestor walk below
        # needs the whole trace's spans indexed first.
        for s in trace_spans:
            task_id, used_conversation_id = _resolve_span_task_id(s, span_by_id)
            span_task_id[s.span_id] = task_id
            span_used_conversation_id[s.span_id] = used_conversation_id
            span_parent_task_id[s.span_id] = _resolve_span_parent_task_id(s, span_by_id)
            if used_conversation_id:
                summary.spans_with_conversation_id += 1
            else:
                summary.spans_fallback_to_trace_id += 1

    kept = [s for s in spans if _kind_of(s) in SUPPORTED_KINDS]
    summary.kept_spans += len(kept)

    # Phase 2 (per resolved task_id): group kept spans by the task_id
    # resolved above -- which can span more than one trace_id (a source
    # that gives one CLI invocation its own trace per turn but a stable
    # session id across turns, confirmed for Hermes) -- and run the
    # lineage-chaining bookkeeping once per task_id group instead of once
    # per trace_id, so step_index stays one continuous, collision-free
    # sequence for the whole task no matter how many physical traces (or,
    # within one trace, how many distinct task_ids -- the subagent case)
    # it's assembled from.
    by_task: dict[str, list[Span]] = {}
    for s in kept:
        by_task.setdefault(span_task_id[s.span_id], []).append(s)

    records: list[dict[str, Any]] = []

    for task_id, task_spans in by_task.items():
        task_spans.sort(key=lambda s: s.start_time_unix_nano)

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

        for span in task_spans:
            kind = _kind_of(span)
            used_conversation_id = span_used_conversation_id[span.span_id]
            parent_task_id = span_parent_task_id[span.span_id]
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

            workflow, workflow_basis = _workflow_of(span, span_by_id)

            if kind == "LLM":
                record = _llm_event(
                    span, task_id, used_conversation_id, parent_task_id, step, parent_step,
                    summary, pricing_context, children_by_parent, workflow, workflow_basis,
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
                    span, task_id, used_conversation_id, parent_task_id, step, parent_step,
                    summary, workflow, workflow_basis,
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

    fill_missing_tool_model(records)
    summary.total_records = len(records)
    return records, summary


def _resolve_span_task_id(span: Span, span_by_id: dict[str, Span]) -> tuple[str, bool]:
    """(task_id, used_conversation_id) for this one span: its own
    gen_ai.conversation.id/session.id if present (preferring
    gen_ai.conversation.id when a span somehow carries both -- not
    expected in practice, they're the same source's own choice of one
    attribute name, not two independent signals meant to disagree), else
    the nearest ancestor's (walking the real parent_span_id chain --
    OTel context propagation means it's common for only a root-ish span,
    not every span, to carry one), else this span's own trace ID.

    Resolved per span, not once for the whole trace -- see module
    docstring point 1 for why (a real task boundary, e.g. a Hermes
    subagent, can start partway through a trace, with every span inside
    it carrying its own distinct, self-reported id). There is no
    "multiple different values across the trace" ambiguity to guess at
    once resolution is per span: each span's own stated identity (or its
    own ancestor's) is trusted directly, never overridden by what an
    unrelated span elsewhere in the trace happens to say about itself.
    """
    own = _first_present(span.attributes, _CONVERSATION_ID_ATTRS)
    if own is not None:
        return str(own), True

    current_id = span.parent_span_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        parent = span_by_id.get(current_id)
        if parent is None:
            break
        value = _first_present(parent.attributes, _CONVERSATION_ID_ATTRS)
        if value is not None:
            return str(value), True
        current_id = parent.parent_span_id

    return span.trace_id, False


def _resolve_span_parent_task_id(span: Span, span_by_id: dict[str, Span]) -> str | None:
    """The real, source-confirmed parent_task_id for this span (see
    _PARENT_TASK_ID_ATTRS): its own value if present, else the nearest
    ancestor's -- the same ancestor-walk pattern as _workflow_of.
    Needed because the attribute lives on a subagent's own wrapping
    AGENT-kind span in real captures (hermes-otel), never converted into
    an Event itself, not on the LLM/TOOL spans nested inside it that
    actually need to carry it forward.
    """
    value = _first_present(span.attributes, _PARENT_TASK_ID_ATTRS)
    if value is not None:
        return str(value)

    current_id = span.parent_span_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        parent = span_by_id.get(current_id)
        if parent is None:
            return None
        value = _first_present(parent.attributes, _PARENT_TASK_ID_ATTRS)
        if value is not None:
            return str(value)
        current_id = parent.parent_span_id
    return None


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


def _workflow_of(span: Span, span_by_id: dict[str, Span]) -> tuple[str, str]:
    """(workflow, workflow_basis): the nearest AGENT/CHAIN ancestor's own
    agent.name attribute when present, else its span name, else
    _NO_WORKFLOW_LABEL when no such ancestor exists at all. Unlike
    task_id, this has no "never guess" constraint -- workflow is
    inherently an approximate label, so a documented heuristic is fine.

    agent.name over the span's own name because at least one real,
    confirmed source (Google ADK) decorates its AGENT-kind span's name
    ("agent_run [search_agent]") while carrying the bare, human-chosen
    name ("search_agent") in this attribute instead -- see _AGENT_NAME_ATTR.
    """
    current_id = span.parent_span_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        parent = span_by_id.get(current_id)
        if parent is None:
            return _NO_WORKFLOW_LABEL, "no_workflow_ancestor"
        if _kind_of(parent) in _WORKFLOW_KINDS:
            name = parent.attributes.get(_AGENT_NAME_ATTR)
            if name:
                return str(name), "agent_name_attribute"
            return parent.name, "span_name"
        current_id = parent.parent_span_id
    return _NO_WORKFLOW_LABEL, "no_workflow_ancestor"


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


def _base_metadata(
    span: Span, masked_spans: int, used_conversation_id: bool, parent_task_id: str | None,
) -> dict[str, Any]:
    metadata = {
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
    # A real, source-confirmed reference to another task this one was
    # delegated from, already resolved (own attribute or nearest ancestor's
    # -- see _resolve_span_parent_task_id) by the caller. Absent for every
    # source that doesn't expose one; never guessed from timing or
    # content, only ever a value the source itself reported.
    if parent_task_id is not None:
        metadata["parent_task_id"] = parent_task_id
    return metadata


def _find_llm_token_donor_child(
    span: Span, children_by_parent: dict[str, list[Span]]
) -> Span | None:
    """A direct child span carrying token usage but no content of its own.
    The shape at least one real exporter produces (hermes-otel, confirmed
    against a real capture): an outer LLM-kind span with full content and
    no gen_ai.usage.* attributes, and its own LLM-kind child span with
    token counts and no content. Both are real OTLP parent/child, not
    siblings needing the temporal-overlap heuristic
    sources/openclaw_localtrace.py's similar split requires. A direct
    child lookup is enough here.

    None whenever no such child exists, which is a no-op for any source
    where a single span already carries both (the common case this
    adapter was built for).
    """
    for child in children_by_parent.get(span.span_id, ()):
        if _kind_of(child) != "LLM":
            continue
        if _first_present(child.attributes, (_INPUT_ATTR,)) is not None:
            continue
        if (
            _first_present(child.attributes, _TOKENS_IN_ATTRS) is None
            and _first_present(child.attributes, _TOKENS_OUT_ATTRS) is None
        ):
            continue
        return child
    return None


def _llm_event(
    span: Span,
    task_id: str,
    used_conversation_id: bool,
    parent_task_id: str | None,
    step: int,
    parent_step: int | None,
    summary: ConversionSummary,
    pricing_context: pricing.PricingContext,
    children_by_parent: dict[str, list[Span]],
    workflow: str,
    workflow_basis: str,
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
    cache_read = _first_present(span.attributes, _CACHE_READ_TOKEN_ATTRS)
    cache_write = _first_present(span.attributes, _CACHE_WRITE_TOKEN_ATTRS)

    if tokens_in is None and tokens_out is None:
        donor = _find_llm_token_donor_child(span, children_by_parent)
        if donor is not None:
            tokens_in = _first_present(donor.attributes, _TOKENS_IN_ATTRS)
            tokens_out = _first_present(donor.attributes, _TOKENS_OUT_ATTRS)
            cache_read = _first_present(donor.attributes, _CACHE_READ_TOKEN_ATTRS)
            cache_write = _first_present(donor.attributes, _CACHE_WRITE_TOKEN_ATTRS)
            if model is None:
                model = _first_present(donor.attributes, _MODEL_ATTRS)
            summary.merged_token_child_spans += 1

    cost = _first_present(span.attributes, _COST_ATTRS)
    metadata = _base_metadata(span, mask_count, used_conversation_id, parent_task_id)
    metadata["workflow_basis"] = workflow_basis
    cost_usd = float(cost) if cost is not None else None
    if cost_usd is None:
        estimated = pricing.estimate_cost_usd(
            pricing_context.entries,
            model,
            tokens_in=int(tokens_in) if tokens_in is not None else None,
            tokens_out=int(tokens_out) if tokens_out is not None else None,
            cache_read_tokens=int(cache_read) if cache_read is not None else None,
            cache_write_tokens=int(cache_write) if cache_write is not None else None,
        )
        if estimated is not None:
            cost_usd = estimated
            metadata["cost_basis"] = "estimated_from_bundled_pricing_table"
            summary.pricing_table_generated_at = pricing_context.generated_at

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
        "cost_usd": cost_usd,
        "model": model,
        "parent_id": parent_step,
        "workflow": workflow,
        "metadata": metadata,
    }


def _tool_events(
    span: Span,
    task_id: str,
    used_conversation_id: bool,
    parent_task_id: str | None,
    step: int,
    parent_step: int | None,
    summary: ConversionSummary,
    workflow: str,
    workflow_basis: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw_input = _first_present(span.attributes, (_INPUT_ATTR,))
    if raw_input is None:
        return None, None

    structured_in = _is_structured(span.attributes, _INPUT_MIME_ATTR, raw_input)
    call_hash, call_masks = hashing.content_hash(raw_input, structured=structured_in)
    _record_mask_stats(summary, call_masks)

    tool_name = _first_present(span.attributes, _TOOL_NAME_ATTRS) or span.name

    call_metadata = _base_metadata(span, call_masks, used_conversation_id, parent_task_id)
    call_metadata["workflow_basis"] = workflow_basis

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
        # Not a call-level fact -- a tool call has no model of its own.
        # Filled in by model_inference.fill_missing_tool_model() once
        # every record in this task exists, from the nearest LLM-kind
        # span in true chronological order, in either direction -- see
        # metadata.model_basis and docs/schema.md.
        "model": None,
        "parent_id": parent_step,
        "workflow": workflow,
        "metadata": call_metadata,
    }

    raw_output = _first_present(span.attributes, (_OUTPUT_ATTR,))
    if raw_output is None:
        return call, None

    structured_out = _is_structured(span.attributes, _OUTPUT_MIME_ATTR, raw_output)
    result_hash, result_masks = hashing.content_hash(raw_output, structured=structured_out)
    _record_mask_stats(summary, result_masks)

    result_metadata = _base_metadata(span, result_masks, used_conversation_id, parent_task_id)
    result_metadata["workflow_basis"] = workflow_basis

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
        "model": None,  # filled in by model_inference.fill_missing_tool_model()
        "parent_id": None,  # filled in by convert()
        "workflow": workflow,
        "metadata": result_metadata,
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
