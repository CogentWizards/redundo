"""Claude Cowork native OTLP telemetry -> redundo.analyzer Event schema.

Cowork's telemetry is architecturally different from Claude Code's, not
just differently named -- there is no span tree at all in the common
(first-party account) case. Cowork "exports events via the OTel
logs/events protocol" exclusively; `trace_id`/`span_id` correlation only
exists on third-party deployments with the beta `otlpTracesEnabled`
setting, whose span shape isn't documented anywhere this adapter could
verify it against, so it isn't attempted here -- this module converts the
universal, always-present logs-only signal.

Six event types, correlated purely by attributes rather than real parent-
child span structure: `session.id` (task grouping), `prompt.id` (links
every event produced while processing one user prompt -- Cowork's
equivalent of Claude Code's `claude_code.interaction` span, but as a
shared attribute value, not a span), and `event.sequence` (a monotonic
per-session counter that is the *only* ordering signal available -- there
is no start/end timestamp pair to reconstruct real concurrency from, so
there is nothing to chain against and no ancestor to resolve). Given that,
this adapter does not attempt any parent/child reconstruction at all:
every record's `parent_id` is left None, and `redundo.analyzer.lineage`'s
own documented linear-fallback (each event's effective parent is simply
the immediately preceding event in the same task) does all the lineage
work -- correctly, since a flat, ordered event stream with no branch
information is exactly the case that fallback exists for.

Event -> schema mapping:
  - `api_request` -> llm_call. Unlike Claude Code, cost/tokens/model are
    all directly on this one event -- no cross-event join needed for
    cost_usd. `assistant_response` (joined by the shared `request_id`)
    supplies `metadata.response_hash` when present -- something Claude
    Code's native telemetry can never provide (its llm_request spans
    never carry response content, under any flag). `user_prompt` (joined
    by `prompt.id`, first api_request/api_error per prompt.id only, same
    reasoning as sources.claude_code's first-completion-per-turn logic)
    supplies the call's own content_hash.
  - `api_error` -> also llm_call, with outcome="error" -- a failed
    request is still something that happened and cost attention, and
    skipping it would make failed-request loops invisible.
  - `tool_result` -> tool_call only, never a paired tool_result record.
    `tool_result` carries `tool_input` (arguments, for every tool
    including MCP -- no positional-join fragility needed here, since the
    call and its outcome are the *same* record) but never any output
    content field, under any documented configuration -- this is a
    structural limit of what Cowork exports, not a logging-level gap like
    Claude Code's built-in tools. Because there's genuinely no result
    content ever, and a fabricated result content_hash is unsafe (see
    sources.claude_code's extensive comment on why -- the same
    reasoning applies verbatim), outcome is set directly on the tool_call
    record instead of on a synthetic result, so terminal_outcome tracking
    isn't lost entirely even though result-identity comparison is.
  - `tool_decision` with decision="reject" -> tool_call (outcome="error"),
    representing an attempt that was blocked before it ever ran and so
    has no corresponding tool_result at all. "accept" decisions are never
    converted from this event -- the same call's tool_result covers it;
    converting both would double-count every successfully executed call.
  - `user_prompt` -- consumed only as a content source, never emitted as
    its own record (no schema event_type fits "a user typed something").

See docs/cowork.md for the full written contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import hashing

from ..otlp import LogRecord, parse_log_records

_SESSION_ID_ATTR = "session.id"
_REDACTED = "<REDACTED>"


@dataclass
class CoworkConversionSummary:
    total_log_records: int = 0
    sessions_total: int = 0
    total_records: int = 0
    skipped_by_kind: dict[str, int] = field(default_factory=dict)
    records_with_prompt_content: int = 0
    records_with_tool_input_content: int = 0
    records_with_opaque_content: int = 0
    records_with_response_hash: int = 0

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
        out.append(
            "tool_result content is never available from Cowork's telemetry, under any "
            "documented configuration -- every tool_call from this adapter has no paired "
            "tool_result; result_signal reads UNKNOWN for every tool candidate pair, "
            "always. This is a structural limit of the source, not a flag you're missing."
        )
        return out


def convert_cowork(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], CoworkConversionSummary]:
    """documents: parsed OTLP JSON logs export documents (resourceLogs).
    A trace document handed to this function is silently ignored -- use
    sources.claude_code.convert_claude_code() for Claude Code's trace-based
    telemetry instead.
    """
    summary = CoworkConversionSummary()

    all_records: list[LogRecord] = []
    for doc in documents:
        if "resourceLogs" in doc:
            recs = parse_log_records(doc)
            all_records.extend(recs)
            summary.total_log_records += len(recs)

    by_session: dict[str, list[LogRecord]] = {}
    for rec in all_records:
        session_id = rec.attributes.get(_SESSION_ID_ATTR)
        if not session_id:
            continue  # unattributable -- nothing to group it under
        by_session.setdefault(session_id, []).append(rec)

    records: list[dict[str, Any]] = []
    for session_id, session_records in by_session.items():
        summary.sessions_total += 1
        records.extend(_convert_session(session_id, session_records, summary))

    summary.total_records = len(records)
    return records, summary


def _sort_key(rec: LogRecord) -> tuple[int, str]:
    seq = rec.attributes.get("event.sequence")
    return (int(seq) if seq is not None else 2**62, rec.attributes.get("event.timestamp") or "")


_CONVERTIBLE_EVENTS = {"api_request", "api_error", "tool_result", "tool_decision"}


def _convert_session(
    session_id: str, records: list[LogRecord], summary: CoworkConversionSummary
) -> list[dict[str, Any]]:
    records = sorted(records, key=_sort_key)

    prompt_text_by_prompt_id: dict[str, str] = {}
    response_by_request_id: dict[str, str] = {}
    for rec in records:
        name = rec.attributes.get("event.name")
        if name == "user_prompt":
            prompt = rec.attributes.get("prompt")
            prompt_id = rec.attributes.get("prompt.id")
            if prompt_id and isinstance(prompt, str) and prompt and prompt != _REDACTED:
                prompt_text_by_prompt_id.setdefault(prompt_id, prompt)
        elif name == "assistant_response":
            response = rec.attributes.get("response")
            request_id = rec.attributes.get("request_id")
            if request_id and isinstance(response, str) and response and response != _REDACTED:
                response_by_request_id[request_id] = response

    claimed_prompt_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    step = 0

    for rec in records:
        name = rec.attributes.get("event.name")
        if name not in _CONVERTIBLE_EVENTS:
            if name not in (None, "user_prompt", "assistant_response"):
                summary.skipped_by_kind[name] = summary.skipped_by_kind.get(name, 0) + 1
            continue

        if name == "tool_decision":
            if rec.attributes.get("decision") != "reject":
                continue  # accepted calls are covered by their tool_result instead
            record = _rejected_tool_call(session_id, step, rec, summary)
        elif name == "tool_result":
            record = _tool_call_from_result(session_id, step, rec, summary)
        else:  # api_request / api_error
            is_first = False
            prompt_id = rec.attributes.get("prompt.id")
            if prompt_id and prompt_id not in claimed_prompt_ids:
                claimed_prompt_ids.add(prompt_id)
                is_first = True
            record = _llm_event(
                session_id, step, rec, name, is_first,
                prompt_text_by_prompt_id, response_by_request_id, summary,
            )

        out.append(record)
        step += 1

    return out


def _opaque_hash(rec: LogRecord, salt: str) -> tuple[str, int]:
    """Unique per log record (event.sequence + salt), never coincidentally
    equal to another record's hash -- see sources.claude_code's
    _opaque_hash for the full reasoning (safe for a call's content_hash,
    never used for a result's).
    """
    seq = rec.attributes.get("event.sequence")
    ts = rec.attributes.get("event.timestamp")
    return hashing.content_hash(f"{salt}:{seq}:{ts}", structured=False)


def _timestamp(rec: LogRecord) -> Any:
    """Prefer the `event.timestamp` attribute (an ISO 8601 string, per
    docs/cowork.md) -- fall back to the log record's own OTLP
    `timeUnixNano` field if that attribute is ever absent, so a record
    never ends up with no timestamp at all when real OTLP timing data
    exists.
    """
    ts = rec.attributes.get("event.timestamp")
    if ts:
        return ts
    if rec.time_unix_nano:
        return datetime.fromtimestamp(rec.time_unix_nano / 1e9, tz=timezone.utc).isoformat()
    return None


def _outcome_bool(value: Any) -> str | None:
    if isinstance(value, bool):
        return "ok" if value else "error"
    if isinstance(value, str):
        if value.lower() == "true":
            return "ok"
        if value.lower() == "false":
            return "error"
    return None


def _base_record(
    task_id: str, step: int, event_type: str, name: Any, content_hash: str,
    outcome: str | None, timestamp: Any, cost_usd: float | None, model: Any,
    content_basis: str, masks: int, extra_metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "step_index": step,
        "event_type": event_type,
        "name": name,
        "content_hash": content_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": outcome,
        "timestamp": timestamp,
        "cost_usd": cost_usd,
        "model": model,
        "parent_id": None,
        "workflow": None,  # no agent/subagent field documented for Cowork
        "metadata": {
            "hash_spec": hashing.HASH_SPEC,
            "content_basis": content_basis,
            "masked_spans": masks,
            **extra_metadata,
        },
    }


def _llm_event(
    task_id: str,
    step: int,
    rec: LogRecord,
    event_name: str,
    is_first_of_prompt: bool,
    prompt_text_by_prompt_id: dict[str, str],
    response_by_request_id: dict[str, str],
    summary: CoworkConversionSummary,
) -> dict[str, Any]:
    model = rec.attributes.get("model")
    request_id = rec.attributes.get("request_id")
    prompt_id = rec.attributes.get("prompt.id")

    prompt = prompt_text_by_prompt_id.get(prompt_id) if is_first_of_prompt and prompt_id else None
    if prompt is not None:
        digest, masks = hashing.content_hash(prompt, structured=False)
        content_basis = "prompt"
        summary.records_with_prompt_content += 1
    else:
        digest, masks = _opaque_hash(rec, "llm")
        content_basis = "opaque"
        summary.records_with_opaque_content += 1

    input_tokens = rec.attributes.get("input_tokens")
    output_tokens = rec.attributes.get("output_tokens")
    cache_read = rec.attributes.get("cache_read_tokens") or 0
    cache_creation = rec.attributes.get("cache_creation_tokens") or 0
    tokens_in = (
        (input_tokens or 0) + cache_read + cache_creation if input_tokens is not None else None
    )
    cost_usd = rec.attributes.get("cost_usd")

    extra_meta: dict[str, Any] = {"request_id": request_id}
    response_hash = None
    if request_id:
        response_text = response_by_request_id.get(request_id)
        if response_text is not None:
            response_hash, _ = hashing.content_hash(response_text, structured=False)
            summary.records_with_response_hash += 1
    if response_hash is not None:
        extra_meta["response_hash"] = response_hash

    record = _base_record(
        task_id, step, "llm_call", model, digest,
        "ok" if event_name == "api_request" else "error",
        _timestamp(rec), float(cost_usd) if cost_usd is not None else None,
        model, content_basis, masks, extra_meta,
    )
    record["tokens_in"] = int(tokens_in) if tokens_in is not None else None
    record["tokens_out"] = int(output_tokens) if output_tokens is not None else None
    return record


def _tool_call_from_result(
    task_id: str, step: int, rec: LogRecord, summary: CoworkConversionSummary
) -> dict[str, Any]:
    tool_name = rec.attributes.get("tool_name")
    tool_input = rec.attributes.get("tool_input")
    is_mcp = False

    if isinstance(tool_input, str) and tool_input:
        parsed = _try_parse_json(tool_input)
        content: Any = parsed if parsed is not None else tool_input
        digest, masks = hashing.content_hash(content, structured=True)
        content_basis = "tool_input"
        summary.records_with_tool_input_content += 1
    else:
        digest, masks = _opaque_hash(rec, "tool")
        content_basis = "opaque"
        summary.records_with_opaque_content += 1

    params = rec.attributes.get("tool_parameters")
    parsed_params = _try_parse_json(params) if isinstance(params, str) else None
    if isinstance(parsed_params, dict) and parsed_params.get("mcp_server_name"):
        is_mcp = True
        if not (isinstance(tool_name, str) and tool_name.startswith("mcp__")):
            tool_name = f"mcp__{parsed_params['mcp_server_name']}__{parsed_params.get('mcp_tool_name', tool_name)}"

    return _base_record(
        task_id, step, "tool_call", tool_name, digest,
        _outcome_bool(rec.attributes.get("success")),
        _timestamp(rec), None, None, content_basis, masks,
        {
            "mcp": is_mcp,
            "decision_type": rec.attributes.get("decision_type"),
            "decision_source": rec.attributes.get("decision_source"),
        },
    )


def _rejected_tool_call(
    task_id: str, step: int, rec: LogRecord, summary: CoworkConversionSummary
) -> dict[str, Any]:
    digest, masks = _opaque_hash(rec, "tool-rejected")
    summary.records_with_opaque_content += 1
    return _base_record(
        task_id, step, "tool_call", rec.attributes.get("tool_name"), digest,
        "error",  # rejected -- never executed
        _timestamp(rec), None, None, "opaque", masks,
        {"decision_source": rec.attributes.get("source"), "rejected_before_execution": True},
    )


def _try_parse_json(value: str) -> Any | None:
    import json

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
