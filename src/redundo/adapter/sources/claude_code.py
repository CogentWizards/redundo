"""Claude Code native OTLP telemetry -> redundo.analyzer Event schema.

Claude Code's span shape differs from OpenInference in ways that change
the conversion strategy, not just the attribute names:

1. task_id is `session.id` -- present on every span (per Anthropic's docs)
   and stable across a whole CLI session, which typically spans many user
   turns. Each turn (`claude_code.interaction`) is its own OTLP trace with
   no real parent linking it to the previous turn's trace, so cross-turn
   redundancy (e.g. re-reading a file two turns after the first read)
   would be invisible if lineage only followed real parent_span_id. Fixed
   the same way redundo.analyzer.lineage already handles a source with no
   parent_id at all: step_index is assigned once, globally, across the
   whole session (not reset per turn), and a span with no real kept
   ancestor gets parent_id=None -- the analyzer's own documented linear
   fallback then chains it to the immediately preceding event in the
   session, bridging turns for free. No adapter-side cross-trace stitching
   needed.

2. One `claude_code.tool` span represents an entire call+result, unlike
   OpenInference's separate TOOL call/result spans. Split into a
   synthetic (tool_call, tool_result) pair here, mirroring
   sources.openinference's _tool_events() shape, using:
     - outcome: the child `claude_code.tool.execution` span's `success`
       attribute (falling back to `claude_code.tool.blocked_on_user`'s
       `decision` when execution never started).
     - call content: `file_path` / `full_command` / other
       OTEL_LOG_TOOL_DETAILS-gated attributes on the span itself.
     - result content: the `tool.output` span *event* (OTEL_LOG_TOOL_CONTENT
       -- a nested, timestamped record inside the span, not an attribute).

3. MCP tool call arguments are not observable on the trace span at any
   logging level -- confirmed empirically, not assumed from docs. They
   only exist on the *logs* signal's `claude_code.tool_result` record
   (`tool_input`), joined back to its span via the shared `tool_use_id`
   field (this join does not depend on trace_id/span_id log correlation,
   which requires Claude Code >= 2.1.212). convert_claude_code() accepts
   log documents for exactly this reason -- traces alone cannot produce a
   content_hash for MCP tool calls.

4. `claude_code.llm_request` spans carry no prompt/response text, ever,
   under any flag combination. The only text available for hashing an
   llm_call is the *interaction's* `user_prompt` attribute -- usable only
   for the first llm_request that responds directly to it (real parent ==
   the interaction span); later completions in the same tool-use loop have
   no content to hash. Rather than dropping those records (losing their
   token/cost contribution, which matters for "where did my tokens go"),
   they're kept with an opaque, span_id-derived content_hash that can
   never coincidentally match another record's hash -- visible to cost/
   coverage accounting, structurally inert for candidate-pair matching.
   Every such record's metadata marks this explicitly
   (`content_basis: "opaque"` vs `"prompt"` / `"tool_input"` / etc.) so a
   reader can tell which repeats were actually checked against real
   content versus which just couldn't collide with anything.

5. `metadata.write` is never set. No source data indicates whether a
   built-in tool call (let alone an MCP one) mutated anything -- inferring
   it from tool_name ("Bash" might be `ls` or `rm -rf`) would be a guess,
   not a finding. This intentionally leaves tool_call write status
   Signal.UNKNOWN in the analyzer for every record from this adapter,
   consistent with "absence is not a claim of a default value."

See docs/claude-code.md for the full written contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import hashing
from ..otlp import LogRecord, Span, parse_log_records, parse_spans

_KEPT_SPAN_NAMES = {"claude_code.llm_request", "claude_code.tool"}
_STRUCTURAL_SPAN_NAMES = {
    "claude_code.interaction",
    "claude_code.tool.execution",
    "claude_code.tool.blocked_on_user",
}
# Any other span name (claude_code.hook, future additions) is counted in
# skipped_by_kind rather than silently dropped.

_SESSION_ID_ATTR = "session.id"
_REDACTED = "<REDACTED>"


@dataclass
class ConversionSummary:
    total_spans: int = 0
    total_log_records: int = 0
    sessions_total: int = 0
    sessions_with_session_id: int = 0
    kept_spans: int = 0
    skipped_by_kind: dict[str, int] = field(default_factory=dict)
    total_records: int = 0
    # content provenance, for the same reason sources.openinference tracks
    # masked_spans: "zero repeats found" and "zero repeats because content
    # wasn't observable" must not look identical in the output.
    records_with_prompt_content: int = 0
    records_with_windowed_prompt_content: int = 0  # subset of records_with_prompt_content
    records_with_tool_input_content: int = 0
    records_with_opaque_content: int = 0
    mcp_tool_calls_total: int = 0
    mcp_tool_calls_with_arguments: int = 0
    sessions_without_interaction_span: int = 0

    def notes(self) -> list[str]:
        out = []
        if self.total_records == 0:
            out.append("no records produced -- nothing below is meaningful.")
            return out
        out.append(
            f"{self.records_with_opaque_content}/{self.total_records} record(s) "
            f"({self.records_with_opaque_content / self.total_records:.0%}) have no "
            "observable content -- kept for cost/coverage accounting, but structurally "
            "cannot match anything (see metadata.content_basis)."
        )
        if self.mcp_tool_calls_total:
            out.append(
                f"{self.mcp_tool_calls_with_arguments}/{self.mcp_tool_calls_total} "
                f"({self.mcp_tool_calls_with_arguments / self.mcp_tool_calls_total:.0%}) "
                "MCP tool call(s) had arguments available -- requires the logs signal "
                "(OTEL_LOGS_EXPORTER=otlp) to be captured alongside traces; MCP call "
                "arguments are never present on the trace span alone."
            )
        if self.sessions_without_interaction_span:
            out.append(
                f"{self.sessions_without_interaction_span}/{self.sessions_total} session(s) "
                "had no claude_code.interaction span at all (Agent SDK / streaming "
                "transport, not direct `claude -p`/interactive sessions -- see docs/claude-code.md). "
                f"{self.records_with_windowed_prompt_content} llm_call record(s) recovered "
                "prompt content via time-window correlation against the logs signal's "
                "user_prompt events instead (metadata.content_basis='prompt_windowed') -- "
                "a heuristic, not an exact match; see docs/claude-code.md for what can break it."
            )
        return out


def convert_claude_code(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ConversionSummary]:
    """documents: a mix of parsed OTLP JSON export documents -- some traces
    (resourceSpans), some logs (resourceLogs), in any order. Both are
    needed for a complete conversion (see module docstring point 3); traces
    alone still produce a valid, honestly-degraded corpus.
    """
    summary = ConversionSummary()

    all_spans: list[Span] = []
    all_log_records: list[LogRecord] = []
    for doc in documents:
        if "resourceSpans" in doc:
            spans = parse_spans(doc)
            all_spans.extend(spans)
            summary.total_spans += len(spans)
        elif "resourceLogs" in doc:
            records = parse_log_records(doc)
            all_log_records.extend(records)
            summary.total_log_records += len(records)
        # else: not a recognized OTLP export shape; silently ignored --
        # the CLI layer is responsible for only handing convert() files
        # that passed is_trace_document/is_log_document.

    tool_results_by_session = _index_tool_result_logs_by_session(all_log_records)
    api_requests_by_request_id = _index_api_request_logs(all_log_records)
    user_prompts_by_session = _index_user_prompt_logs_by_session(all_log_records)

    by_session: dict[str, list[Span]] = {}
    for span in all_spans:
        session_id = span.attributes.get(_SESSION_ID_ATTR)
        by_session.setdefault(session_id or f"__no_session__:{span.trace_id}", []).append(span)

    records: list[dict[str, Any]] = []
    for session_id, session_spans in by_session.items():
        used_real_session_id = not session_id.startswith("__no_session__:")
        summary.sessions_total += 1
        if used_real_session_id:
            summary.sessions_with_session_id += 1
        task_id = session_id if used_real_session_id else session_spans[0].trace_id
        ordered_tool_results = tool_results_by_session.get(session_id, [])
        ordered_user_prompts = user_prompts_by_session.get(session_id, [])
        records.extend(
            _convert_session(
                task_id, used_real_session_id, session_spans, ordered_tool_results,
                api_requests_by_request_id, ordered_user_prompts, summary,
            )
        )

    summary.total_records = len(records)
    return records, summary


def _log_record_sort_key(rec: LogRecord) -> tuple[int, str]:
    seq = rec.attributes.get("event.sequence")
    return (int(seq) if seq is not None else 2**62, rec.attributes.get("event.timestamp") or "")


def _index_tool_result_logs_by_session(log_records: list[LogRecord]) -> dict[str, list[LogRecord]]:
    """Group `claude_code.tool_result` log records by session, in
    chronological order (`event.sequence`, a monotonic per-session
    counter across every log record type -- falls back to the string
    timestamp when absent). This is a *positional* correlation, not an ID
    join: as of Claude Code 2.1.131 neither `claude_code.tool` nor its
    child spans carry `tool_use_id`/`gen_ai.tool.call.id` at all, despite
    the published spec listing them -- confirmed empirically, not assumed.
    _tool_events() pairs the Nth tool span (by start time) in a session
    with the Nth entry here, cross-checking tool_name as a safety net
    against reordering. See docs/claude-code.md for what breaks this assumption (true
    parallel tool calls within one turn) and how it degrades (unmatched
    entries fall back to opaque content, never a wrong match).
    """
    by_session: dict[str, list[LogRecord]] = {}
    for rec in log_records:
        if rec.attributes.get("event.name") != "tool_result":
            continue
        session_id = rec.attributes.get(_SESSION_ID_ATTR)
        if not session_id:
            continue  # unattributable -- can't join what we can't place
        by_session.setdefault(session_id, []).append(rec)

    for recs in by_session.values():
        recs.sort(key=_log_record_sort_key)
    return by_session


def _index_user_prompt_logs_by_session(log_records: list[LogRecord]) -> dict[str, list[LogRecord]]:
    """Group `claude_code.user_prompt` log records by session, sorted
    chronologically -- the fallback prompt-content source for sessions
    with no `claude_code.interaction` span at all (Agent SDK / streaming
    transport; see _resolve_windowed_prompts()). Same sort key as
    _index_tool_result_logs_by_session().
    """
    by_session: dict[str, list[LogRecord]] = {}
    for rec in log_records:
        if rec.attributes.get("event.name") != "user_prompt":
            continue
        session_id = rec.attributes.get(_SESSION_ID_ATTR)
        if not session_id:
            continue
        by_session.setdefault(session_id, []).append(rec)

    for recs in by_session.values():
        recs.sort(key=_log_record_sort_key)
    return by_session


def _parse_iso_to_nano(ts: Any) -> int | None:
    """`event.timestamp` is an ISO 8601 string on the logs signal (unlike
    the trace signal's integer start_time_unix_nano) -- parsed here only
    for direct comparison against real span start times, in
    _resolve_windowed_prompts(). Returns None on anything unparseable
    rather than raising -- a single malformed timestamp shouldn't abort
    the whole conversion.
    """
    if not isinstance(ts, str) or not ts:
        return None
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return int(dt.timestamp() * 1e9)


def _resolve_windowed_prompts(
    user_prompt_records: list[LogRecord], llm_request_spans: list[Span]
) -> dict[str, str]:
    """Fallback prompt-content source for a session with no
    `claude_code.interaction` span at all (see module docstring and
    docs/claude-code.md's "Agent SDK" entry) -- there is no ID linking a
    `claude_code.llm_request` span back to the `user_prompt` log record
    that triggered it (no `prompt.id` on the span, confirmed empirically),
    so this falls back to time-window bucketing instead: each
    `user_prompt` record marks the start of a new turn, running until the
    next `user_prompt` record's timestamp (or the end of the session for
    the last one). The earliest `llm_request` span (by start time) whose
    own start falls inside a given window is that turn's first
    completion, and gets the window's prompt text -- same "only the first
    completion of a turn" rule as the interaction-span-based path, just
    without a real span to hang the boundary on.

    Only ever called for a session confirmed to have zero
    `claude_code.interaction` spans (see _convert_session) -- a session
    with real interaction spans always uses the precise, ID-exact path
    instead; this heuristic never overrides or competes with it.

    Returns {llm_request span_id -> prompt text} for the (at most one per
    window) spans that win a window.
    """
    windows: list[tuple[int, int, str]] = []
    for i, rec in enumerate(user_prompt_records):
        prompt = rec.attributes.get("prompt")
        if not (isinstance(prompt, str) and prompt and prompt != _REDACTED):
            continue
        start = _parse_iso_to_nano(rec.attributes.get("event.timestamp"))
        if start is None:
            continue
        end = 2**63  # last window runs to +infinity
        for later in user_prompt_records[i + 1 :]:
            later_start = _parse_iso_to_nano(later.attributes.get("event.timestamp"))
            if later_start is not None:
                end = later_start
                break
        windows.append((start, end, prompt))

    if not windows:
        return {}
    windows.sort(key=lambda w: w[0])

    result: dict[str, str] = {}
    claimed_windows: set[int] = set()
    for span in sorted(llm_request_spans, key=lambda s: s.start_time_unix_nano):
        if span.parent_span_id is not None:
            continue  # has real parent info; not part of this fallback's target set
        for window_idx, (start, end, prompt) in enumerate(windows):
            if start <= span.start_time_unix_nano < end:
                if window_idx not in claimed_windows:
                    result[span.span_id] = prompt
                    claimed_windows.add(window_idx)
                break
    return result


def _index_api_request_logs(log_records: list[LogRecord]) -> dict[str, LogRecord]:
    """`claude_code.api_request` log records carry `cost_usd`, never present
    on the `claude_code.llm_request` trace span itself -- joined back by
    `request_id`, which Anthropic issues uniquely per API call and which
    both signals record under the same field name (confirmed empirically
    to hold the same value across signals, unlike tool_use_id).
    """
    by_request_id: dict[str, LogRecord] = {}
    for rec in log_records:
        if rec.attributes.get("event.name") != "api_request":
            continue
        request_id = rec.attributes.get("request_id")
        if request_id:
            by_request_id[request_id] = rec
    return by_request_id


def _convert_session(
    task_id: str,
    used_real_session_id: bool,
    spans: list[Span],
    ordered_tool_results: list[LogRecord],
    api_requests_by_request_id: dict[str, LogRecord],
    ordered_user_prompts: list[LogRecord],
    summary: ConversionSummary,
) -> list[dict[str, Any]]:
    children_by_parent: dict[str, list[Span]] = {}
    for s in spans:
        if s.parent_span_id:
            children_by_parent.setdefault(s.parent_span_id, []).append(s)

    interaction_user_prompt: dict[str, str] = {}  # interaction span_id -> user_prompt text
    has_interaction_span = False
    for s in spans:
        if s.name == "claude_code.interaction":
            has_interaction_span = True
            prompt = s.attributes.get("user_prompt")
            if isinstance(prompt, str) and prompt and prompt != _REDACTED:
                interaction_user_prompt[s.span_id] = prompt

    # Fallback path: a session with no claude_code.interaction span at all
    # (Agent SDK / streaming transport -- confirmed empirically, see
    # docs/claude-code.md) has no real structure to resolve llm_call prompt content
    # from at all. Only ever computed/used when has_interaction_span is
    # False for the whole session -- a session that does have interaction
    # spans always uses the precise path above; this heuristic never
    # competes with or overrides it, even for the rare orphan llm_request
    # (e.g. a session-title-generation call) that direct CLI sessions can
    # also show alongside otherwise-normal interaction spans.
    windowed_prompt_by_span_id: dict[str, str] = {}
    if not has_interaction_span:
        summary.sessions_without_interaction_span += 1
        if ordered_user_prompts:
            llm_request_spans = [s for s in spans if s.name == "claude_code.llm_request"]
            windowed_prompt_by_span_id = _resolve_windowed_prompts(
                ordered_user_prompts, llm_request_spans
            )

    kept = [s for s in spans if s.name in _KEPT_SPAN_NAMES]
    for s in spans:
        if s.name not in _KEPT_SPAN_NAMES and s.name not in _STRUCTURAL_SPAN_NAMES:
            summary.skipped_by_kind[s.name] = summary.skipped_by_kind.get(s.name, 0) + 1
    kept.sort(key=lambda s: s.start_time_unix_nano)
    summary.kept_spans += len(kept)

    # Every llm_request within one interaction shares the same direct
    # parent (the interaction span itself -- confirmed empirically, they
    # are not nested progressively round-to-round), so "direct parent is
    # the interaction span" cannot distinguish "the completion that
    # responded to the user" from "a later completion after a tool
    # result came back" -- it's true for both. Only the first llm_request
    # (earliest start_time) under a given interaction actually responds
    # to that interaction's user_prompt; assigning it to every one would
    # give every completion in a multi-round tool-use loop the same
    # content_hash, a false "identical prompt" match between rounds that
    # are actually different. Claim it for the first one only.
    first_llm_request_of_interaction: set[str] = set()
    claimed_interactions: set[str] = set()
    for s in kept:
        if s.name == "claude_code.llm_request" and s.parent_span_id in interaction_user_prompt:
            if s.parent_span_id not in claimed_interactions:
                first_llm_request_of_interaction.add(s.span_id)
                claimed_interactions.add(s.parent_span_id)

    step = 0
    last_step_of_span: dict[str, int] = {}
    chain_tail: dict[str, tuple[int, int, int]] = {}  # ancestor span_id -> (start, end, step)
    records: list[dict[str, Any]] = []
    log_idx = 0  # position into ordered_tool_results; advances once per tool span

    for span in kept:
        # Claude Code's real hierarchy is shallow and known (interaction ->
        # {llm_request, tool} directly; subagent spans -> a parent tool
        # span) -- unlike OpenInference sources, there's no need to walk
        # past several levels of unkept wrapper spans to find a kept
        # ancestor. The wrapper itself (typically the interaction span,
        # which is never kept -- see _STRUCTURAL_SPAN_NAMES) is exactly
        # what every direct child should be chained against: the first
        # child under it starts a new root (parent=None) within this
        # trace, and every next child compares against the previous
        # sibling's watermark, same as _extend_chain_tail already does.
        # Using the real, unwalked parent_span_id as the grouping key
        # (rather than only a kept ancestor) is what makes siblings under
        # an always-unkept interaction span chain to each other at all.
        ancestor_id = span.parent_span_id
        span_end = span.end_time_unix_nano or span.start_time_unix_nano

        if ancestor_id is None:
            parent_step = None
        else:
            tail = chain_tail.get(ancestor_id)
            if tail is not None:
                parent_step = tail[2]
            else:
                # First child under this ancestor. If the ancestor span
                # was itself kept (e.g. a subagent nested under a parent
                # claude_code.tool span), chain to it directly; otherwise
                # (the common case: ancestor is the unkept interaction
                # span) this becomes a new root within the trace.
                parent_step = last_step_of_span.get(ancestor_id)

        if span.name == "claude_code.llm_request":
            record = _llm_event(
                span, task_id, used_real_session_id, step, parent_step,
                interaction_user_prompt, span.span_id in first_llm_request_of_interaction,
                windowed_prompt_by_span_id.get(span.span_id),
                api_requests_by_request_id, summary,
            )
            records.append(record)
            last_step_of_span[span.span_id] = step
            _extend_chain_tail(chain_tail, ancestor_id, span.start_time_unix_nano, span_end, step)
            step += 1

        else:  # claude_code.tool
            log_result = ordered_tool_results[log_idx] if log_idx < len(ordered_tool_results) else None
            log_idx += 1
            call, result = _tool_events(
                span, task_id, used_real_session_id, step, parent_step,
                children_by_parent, log_result, summary,
            )
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
            _extend_chain_tail(
                chain_tail, ancestor_id, span.start_time_unix_nano, span_end, final_step
            )

    return records


def _extend_chain_tail(
    chain_tail: dict[str, tuple[int, int, int]],
    ancestor_id: str | None,
    start: int,
    end: int,
    step: int,
) -> None:
    """Advance the sibling-chaining watermark for `ancestor_id` to this
    span, unconditionally -- unlike sources.openinference's version of
    this function, there's no "only extend forward" overlap guard here.
    Claude Code's own span boundaries overlap even for definitionally
    sequential work (a `claude_code.llm_request` span stays open past the
    point where the tool calls it triggered have already started;
    confirmed empirically -- see docs/claude-code.md), so interval-overlap detection
    would misread ordinary sequential execution as concurrent branching
    and refuse to chain almost every tool call. Kept spans are chained
    purely by start-time order among siblings sharing a real ancestor.
    """
    if ancestor_id is None:
        return
    chain_tail[ancestor_id] = (start, end, step)


def _iso_timestamp(unix_nano: int) -> str:
    return datetime.fromtimestamp(unix_nano / 1e9, tz=timezone.utc).isoformat()


def _outcome_bool(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "ok" if value else "error"
    if isinstance(value, str):
        if value.lower() == "true":
            return "ok"
        if value.lower() == "false":
            return "error"
    return None


def _base_metadata(
    span: Span, used_real_session_id: bool, content_basis: str, extra: dict[str, Any]
) -> dict[str, Any]:
    meta = {
        "hash_spec": hashing.HASH_SPEC,
        "otlp_span_id": span.span_id,
        "otlp_trace_id": span.trace_id,
        "task_id_source": "session_id" if used_real_session_id else "trace_id_fallback",
        "content_basis": content_basis,
    }
    meta.update(extra)
    return meta


def _opaque_hash(span: Span) -> tuple[str, int]:
    """A content_hash derived from the span's own id, not its content --
    unique per span by construction, so it can never coincidentally equal
    another record's hash. See module docstring point 4.
    """
    digest, masks = hashing.content_hash(span.span_id, structured=False)
    return digest, masks


def _llm_event(
    span: Span,
    task_id: str,
    used_real_session_id: bool,
    step: int,
    parent_step: int | None,
    interaction_user_prompt: dict[str, str],
    is_first_of_interaction: bool,
    windowed_prompt: str | None,
    api_requests_by_request_id: dict[str, LogRecord],
    summary: ConversionSummary,
) -> dict[str, Any]:
    model = span.attributes.get("model") or span.attributes.get("gen_ai.request.model")
    request_id = span.attributes.get("request_id")
    api_request_log = api_requests_by_request_id.get(request_id) if request_id else None
    cost_usd = api_request_log.attributes.get("cost_usd") if api_request_log else None
    input_tokens = span.attributes.get("input_tokens")
    output_tokens = span.attributes.get("output_tokens")
    cache_read = span.attributes.get("cache_read_tokens") or 0
    cache_creation = span.attributes.get("cache_creation_tokens") or 0
    tokens_in = (
        (input_tokens or 0) + cache_read + cache_creation
        if input_tokens is not None
        else None
    )

    # user_prompt is only meaningful for the *first* llm_request in this
    # interaction -- the one that actually responds to it. Every
    # llm_request in the interaction shares the same direct parent (see
    # the caller's first_llm_request_of_interaction computation), so that
    # alone can't distinguish them. Prefer the precise, ID-exact
    # interaction-span path; windowed_prompt (time-window bucketed
    # against the logs signal's user_prompt events -- see
    # _resolve_windowed_prompts()) only ever applies to spans the caller
    # has already confirmed have no interaction span to use instead, so
    # there's no ambiguity about which one wins when both are available.
    prompt = None
    content_basis = "opaque"
    if is_first_of_interaction and span.parent_span_id:
        prompt = interaction_user_prompt.get(span.parent_span_id)
        content_basis = "prompt"
    elif windowed_prompt is not None:
        prompt = windowed_prompt
        content_basis = "prompt_windowed"

    if prompt is not None:
        digest, masks = hashing.content_hash(prompt, structured=False)
        summary.records_with_prompt_content += 1
        if content_basis == "prompt_windowed":
            summary.records_with_windowed_prompt_content += 1
    else:
        digest, masks = _opaque_hash(span)
        content_basis = "opaque"
        summary.records_with_opaque_content += 1

    return {
        "task_id": task_id,
        "step_index": step,
        "event_type": "llm_call",
        "name": model or span.name,
        "content_hash": digest,
        "tokens_in": int(tokens_in) if tokens_in is not None else None,
        "tokens_out": int(output_tokens) if output_tokens is not None else None,
        "outcome": _outcome_bool(span.attributes.get("success")),
        "timestamp": _iso_timestamp(span.start_time_unix_nano),
        "cost_usd": float(cost_usd) if cost_usd is not None else None,
        "model": model,
        "parent_id": parent_step,
        "workflow": span.attributes.get("agent_id"),
        "metadata": _base_metadata(
            span, used_real_session_id, content_basis,
            {"masked_spans": masks, "request_id": span.attributes.get("request_id")},
        ),
    }


def _tool_events(
    span: Span,
    task_id: str,
    used_real_session_id: bool,
    step: int,
    parent_step: int | None,
    children_by_parent: dict[str, list[Span]],
    log_result: LogRecord | None,
    summary: ConversionSummary,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    tool_name = span.attributes.get("tool_name") or span.name
    # tool_use_id/gen_ai.tool.call.id would be the reliable join key if
    # present -- they aren't, as of Claude Code 2.1.131 (empirically
    # confirmed; the published spec lists them but they never appear on
    # any span in this version). log_result was matched positionally by
    # the caller; cross-check tool_name here as the only available
    # consistency check before trusting it -- a mismatch means the
    # positional assumption broke (e.g. true parallel tool calls
    # reordering one signal relative to the other), and it's safer to
    # fall back to no-join than to risk attributing the wrong arguments
    # to this call.
    tool_use_id = span.attributes.get("tool_use_id") or span.attributes.get("gen_ai.tool.call.id")
    is_mcp = tool_name == "mcp_tool" or (
        isinstance(tool_name, str) and tool_name.startswith("mcp__")
    )

    if log_result is not None and not _tool_names_compatible(tool_name, log_result):
        log_result = None
    if is_mcp:
        summary.mcp_tool_calls_total += 1
        if log_result is not None and log_result.attributes.get("tool_input"):
            summary.mcp_tool_calls_with_arguments += 1
        # Prefer the qualified name from the log record's tool_parameters
        # (mcp_server_name/mcp_tool_name) when the span only says
        # "mcp_tool" generically; otherwise keep the span's own name,
        # which is often already fully qualified (mcp__server__tool).
        if tool_name == "mcp_tool" and log_result is not None:
            params = log_result.attributes.get("tool_parameters")
            parsed = _try_parse_json(params) if isinstance(params, str) else None
            if isinstance(parsed, dict) and parsed.get("mcp_server_name") and parsed.get("mcp_tool_name"):
                tool_name = f"mcp__{parsed['mcp_server_name']}__{parsed['mcp_tool_name']}"

    call_content, call_basis = _tool_call_content(span, log_result)
    if call_content is not None:
        call_hash, call_masks = hashing.content_hash(call_content, structured=True)
    else:
        call_hash, call_masks = _opaque_hash(span)
    if call_basis == "tool_input":
        summary.records_with_tool_input_content += 1
    elif call_basis == "opaque":
        summary.records_with_opaque_content += 1

    outcome = _tool_outcome(span, children_by_parent)

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
        "workflow": span.attributes.get("agent_id"),
        "metadata": _base_metadata(
            span, used_real_session_id, call_basis,
            {"masked_spans": call_masks, "tool_use_id": tool_use_id, "mcp": is_mcp},
        ),
    }

    result_content = _tool_result_content(span)
    if result_content is None:
        # No tool.output span event at all -- true for every MCP tool
        # call, confirmed empirically: the logs-joined tool_result record
        # carries tool_result_size_bytes (a byte count) but never the
        # actual output, at any logging level. There is no way to emit a
        # content_hash here that's *safe*: unlike an llm_call with no
        # prompt (where a hash that never matches anything is a harmless
        # non-claim), a tool_result's hash is actively compared for
        # equality by classify.py's _result_signal() -- a fabricated
        # per-span hash would read as "result changed" (an active,
        # false claim), and a fabricated constant sentinel would read as
        # "result identical" (also false, and worse: it can feed
        # confirmed_waste). The only safe representation of "unknown
        # result" this schema has is the one sources.openinference
        # already established: no tool_result event at all, so
        # _correlated_result_hash() finds nothing and reports UNKNOWN,
        # not a wrong answer dressed as a confident one.
        return call, None

    result_hash, result_masks = hashing.content_hash(result_content, structured=False)
    result_basis = "prompt"

    result = {
        "task_id": task_id,
        "step_index": None,  # filled in by _convert_session
        "event_type": "tool_result",
        "name": tool_name,
        "content_hash": result_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": outcome,
        "timestamp": _iso_timestamp(span.end_time_unix_nano or span.start_time_unix_nano),
        "cost_usd": None,
        "model": None,
        "parent_id": None,  # filled in by _convert_session
        "workflow": span.attributes.get("agent_id"),
        "metadata": _base_metadata(
            span, used_real_session_id, result_basis,
            {"masked_spans": result_masks, "tool_use_id": tool_use_id, "mcp": is_mcp},
        ),
    }
    return call, result


def _tool_call_content(span: Span, log_result: LogRecord | None) -> tuple[Any, str]:
    """Best available source for what the call's arguments were, in
    preference order: full arguments from the logs join (works for every
    tool, including MCP), then the built-in-tool-specific span attributes
    (file_path, full_command, ...), then nothing.
    """
    if log_result is not None:
        tool_input = log_result.attributes.get("tool_input")
        if isinstance(tool_input, str) and tool_input:
            parsed = _try_parse_json(tool_input)
            return (parsed if parsed is not None else tool_input), "tool_input"

    for key in ("file_path", "full_command", "skill_name", "subagent_type"):
        value = span.attributes.get(key)
        if value:
            return {key: value}, "tool_input"

    return None, "opaque"


# The `tool.output` span event's content attribute key varies per tool --
# confirmed empirically, not documented anywhere: Read uses "content",
# Bash uses "output". Only these two have been directly observed; other
# built-in tools (Edit, Write, Grep, Glob, ...) may use yet another key
# not covered here, in which case this correctly falls through to "no
# content" (opaque/no tool_result) rather than silently returning the
# wrong field -- guessing an unverified key name risks picking up
# unrelated metadata instead of the real output, which is worse than
# under-covering.
_TOOL_OUTPUT_CONTENT_KEYS = ("content", "output")


def _tool_result_content(span: Span) -> Any | None:
    """The only source of actual tool output content: the `tool.output`
    span event (OTEL_LOG_TOOL_CONTENT=1). Confirmed empirically to be the
    *only* place output content ever appears -- the logs-joined
    tool_result record carries tool_input (arguments) but never output
    content, for any tool including MCP ones. Returns None when absent;
    callers must not emit a tool_result record in that case (see
    _tool_events()).
    """
    for event in span.events:
        if event.name == "tool.output":
            for key in _TOOL_OUTPUT_CONTENT_KEYS:
                content = event.attributes.get(key)
                if content is not None:
                    return content
    return None


def _tool_outcome(span: Span, children_by_parent: dict[str, list[Span]]) -> str | None:
    for child in children_by_parent.get(span.span_id, []):
        if child.name == "claude_code.tool.execution":
            outcome = _outcome_bool(child.attributes.get("success"))
            if outcome is not None:
                return outcome
    for child in children_by_parent.get(span.span_id, []):
        if child.name == "claude_code.tool.blocked_on_user":
            decision = child.attributes.get("decision")
            if decision == "reject":
                return "error"
    return None


def _tool_names_compatible(span_tool_name: Any, log_result: LogRecord) -> bool:
    log_tool_name = log_result.attributes.get("tool_name")
    if log_tool_name is None:
        return False
    if log_tool_name == span_tool_name:
        return True
    # MCP calls: the span usually already carries the fully-qualified
    # name (mcp__server__tool); the log record's tool_name is always the
    # generic "mcp_tool" marker, with the real names in tool_parameters.
    # There's no further cross-check possible here -- any mcp__* span
    # paired with a generic "mcp_tool" log entry is accepted.
    if log_tool_name == "mcp_tool" and isinstance(span_tool_name, str) and span_tool_name.startswith("mcp__"):
        return True
    if span_tool_name == "mcp_tool" and isinstance(log_tool_name, str) and log_tool_name.startswith("mcp__"):
        return True
    return False


def _try_parse_json(value: str) -> Any | None:
    import json

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
