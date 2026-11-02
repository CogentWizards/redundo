"""OpenCode sessions -> redundo.analyzer's Event schema (as plain dicts;
see docs/opencode.md).

OpenCode (github.com/anomalyco/opencode, formerly sst/opencode) dropped
its experimental OpenTelemetry flag, so like Pi this reads a native
capture through `AdapterSource.load()`. Two inputs, same data:

- `opencode export <sessionID> > session.json`, one session as
  `{"info": Session, "messages": [{"info": Message, "parts": [Part]}]}`
  (a file, or a directory of them). The scoped option.
- OpenCode's own SQLite database (`opencode db path` prints it), opened
  read-only. Every session in it. Tables `session`, `message` and `part`,
  each row's `data` column holding the message or part JSON minus its ids.

Both are the 1.x ("v1") layout: packages/schema/src/v1/session.ts and
packages/core/src/session/sql.ts at OpenCode's v1.19.5 tag. OpenCode 2.x
keeps sessions in different tables (`session_v2`, `session_message`,
`session_part`); a 2.x-only database is refused with a clear error rather
than half-read.

Five rules that aren't obvious from the code:

1. One assistant *message* is a whole turn, not one model call. Each
   `step-start` ... `step-finish` span inside it is one call, with its own
   cost and tokens on the step-finish part, so that's the llm_call grain.
   The message's own cost/tokens are the sum of its steps and are only
   used for a message with no step parts at all (one that failed before
   its first step started).
2. Tool parts belong to the step they appear in, and carry their own
   result: `state.status` is completed (output), error (error text),
   or pending/running (interrupted, no result). Sessions are linear (a
   revert truncates rather than branches), so records chain in order:
   llm_call, then each tool_call followed by its tool_result, then the
   next step's llm_call.
3. An llm_call's content_hash is the model-visible context for that step,
   rebuilt the way OpenCode's filterCompacted() builds it: everything
   before the step, cut at the latest completed compaction (the compaction
   request, its summary, the retained tail from `tail_start_id`, then
   everything after). Ids, timestamps, snapshots and per-part metadata are
   dropped first. The similarity fingerprint covers only what's new for
   that step: the user's message for a turn's first step, or the previous
   step's tool calls and results.
4. `--sanitize`d exports replace content with per-part tokens like
   `[redacted:tool-output:prt_...]`. Hashing those would make identical
   calls look different, which classify.py would read as "the result
   changed", an active false claim. Redacted content is hashed as opaque
   instead (unique by construction, never comparable).
5. Workflow is the message's own `agent` (build, plan, a subagent's
   name, compaction), a real per-message fact. A subagent runs in its own
   child session, linked by `session.parentID` as
   parent_task_id/parent_task_link_kind="delegation". A step whose
   `patch` part lists changed files and that ran exactly one tool marks
   that tool_call write=True (metadata.write_basis="step_patch_single_tool"):
   OpenCode's own snapshot diff says the step changed files, and only one
   call could have. With several tools in the step, which one wrote is
   unknown, so none is marked; a step with no patch part says nothing
   either way (snapshots only see tracked files, and can be off).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import hashing, pricing
from ..base import AdapterSource, Detection, InputLoadError

SOURCE_NAME = "opencode"
_DOC_MARKER = "opencode_session"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_V1_TABLES = frozenset({"session", "message", "part"})
_V2_TABLES = frozenset({"session_v2", "session_message"})
_NO_WORKFLOW_LABEL = "main"
_REDACTED_PREFIX = "[redacted:"


@dataclass
class ConversionSummary:
    sessions_total: int = 0
    subagent_sessions: int = 0
    total_records: int = 0
    llm_calls: int = 0
    llm_calls_priced_by_opencode: int = 0
    llm_calls_priced_by_bundled_table: int = 0
    llm_calls_unpriced: int = 0
    tool_calls: int = 0
    tool_calls_without_result: int = 0
    tool_calls_marked_write: int = 0
    patch_steps_with_several_tools: int = 0
    retry_parts: int = 0
    records_with_redacted_content: int = 0
    origins: dict[str, int] = field(default_factory=dict)
    pricing_table_generated_at: str | None = None

    def notes(self) -> list[str]:
        out = []
        if self.total_records == 0:
            out.append("no records produced -- nothing below is meaningful.")
            return out
        origins = ", ".join(f"{n} from {o}" for o, n in sorted(self.origins.items()))
        out.append(
            f"{self.sessions_total} OpenCode session(s) ({origins}), "
            f"{self.subagent_sessions} of them subagent sessions: {self.llm_calls} "
            f"llm_call(s) (one per step), {self.tool_calls} tool_call(s)."
        )
        if self.llm_calls:
            out.append(
                f"{self.llm_calls_priced_by_opencode}/{self.llm_calls} llm_call(s) priced by "
                f"OpenCode itself, {self.llm_calls_priced_by_bundled_table} estimated from the "
                "bundled pricing table (OpenCode reported $0 with nonzero tokens), "
                f"{self.llm_calls_unpriced} unpriced."
            )
        if self.tool_calls_marked_write or self.patch_steps_with_several_tools:
            out.append(
                f"{self.tool_calls_marked_write} tool_call(s) marked as writes from OpenCode's "
                f"own file-change snapshot; {self.patch_steps_with_several_tools} step(s) changed "
                "files but ran several tools, so which one wrote is unknown (see docs/opencode.md)."
            )
        if self.tool_calls_without_result:
            out.append(
                f"{self.tool_calls_without_result} tool_call(s) never finished (pending or "
                "running when the turn stopped) and have no tool_result record."
            )
        if self.retry_parts:
            out.append(
                f"{self.retry_parts} failed API attempt(s) OpenCode retried inside a step. "
                "They carry no usage, so they aren't records of their own."
            )
        if self.records_with_redacted_content:
            out.append(
                f"{self.records_with_redacted_content} record(s) came from a --sanitize'd "
                "export, so their content was hashed as opaque and can't match anything. "
                "Export without --sanitize to compare content (only hashes leave adapt)."
            )
        if self.pricing_table_generated_at:
            note = pricing.pricing_staleness_note(self.pricing_table_generated_at)
            if note:
                out.append(note)
        return out


# --- loading -----------------------------------------------------------------


def load_opencode(path: Path) -> list[dict[str, Any]] | None:
    if path.is_file():
        if _is_sqlite(path):
            return _load_database(path)
        if path.suffix == ".json":
            doc = _load_export(path)
            return [doc] if doc else None
        return None
    if path.is_dir():
        documents = [d for d in (_load_export(p) for p in sorted(path.glob("*.json"))) if d]
        return documents or None
    return None


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _load_export(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not (
        isinstance(data, dict)
        and isinstance(data.get("info"), dict)
        and str(data["info"].get("id", "")).startswith("ses")
        and isinstance(data.get("messages"), list)
    ):
        return None
    messages = [
        m for m in data["messages"]
        if isinstance(m, dict) and isinstance(m.get("info"), dict)
    ]
    return {_DOC_MARKER: data["info"], "messages": messages, "origin": "export"}


def _load_database(path: Path) -> list[dict[str, Any]] | None:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise InputLoadError(f"{path}: could not open SQLite database read-only ({exc})") from None
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _V1_TABLES <= tables:
            if _V2_TABLES <= tables:
                raise InputLoadError(
                    f"{path}: this is an OpenCode 2.x database (session_v2/session_message "
                    "tables), which isn't supported yet. OpenCode 1.x databases and "
                    "`opencode export` files are."
                )
            return None
        sessions = conn.execute(
            "SELECT id, parent_id, agent, time_created FROM session ORDER BY time_created, id"
        ).fetchall()
        messages_by_session: dict[str, list[dict[str, Any]]] = {}
        by_message_id: dict[str, dict[str, Any]] = {}
        for msg_id, session_id, time_created, data in conn.execute(
            "SELECT id, session_id, time_created, data FROM message ORDER BY session_id, time_created, id"
        ):
            info = {**_json_or_empty(data, path, "message", msg_id), "id": msg_id, "sessionID": session_id}
            info.setdefault("time", {}).setdefault("created", time_created)
            message = {"info": info, "parts": []}
            by_message_id[msg_id] = message
            messages_by_session.setdefault(session_id, []).append(message)
        for part_id, message_id, data in conn.execute(
            "SELECT id, message_id, data FROM part ORDER BY message_id, id"
        ):
            message = by_message_id.get(message_id)
            if message is not None:
                message["parts"].append({**_json_or_empty(data, path, "part", part_id), "id": part_id})
    except sqlite3.Error as exc:
        raise InputLoadError(f"{path}: could not read OpenCode database ({exc})") from None
    finally:
        conn.close()

    documents = []
    for session_id, parent_id, agent, time_created in sessions:
        info = {"id": session_id, "parentID": parent_id, "agent": agent, "time": {"created": time_created}}
        documents.append({
            _DOC_MARKER: info,
            "messages": messages_by_session.get(session_id, []),
            "origin": "database",
        })
    return documents or None


def _json_or_empty(data: Any, path: Path, kind: str, row_id: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        raise InputLoadError(f"{path}: {kind} {row_id}: data column is not valid JSON") from None
    return value if isinstance(value, dict) else {}


# --- conversion --------------------------------------------------------------


def convert_opencode(
    documents: list[dict[str, Any]],
    *,
    pricing_context: pricing.PricingContext | None = None,
) -> tuple[list[dict[str, Any]], ConversionSummary]:
    summary = ConversionSummary()
    if pricing_context is None:
        pricing_context = pricing.load_pricing_context()
    records: list[dict[str, Any]] = []
    seen_sessions: set[str] = set()
    for doc in documents:
        if _DOC_MARKER not in doc:
            continue
        session_id = doc[_DOC_MARKER].get("id")
        # The same session exported twice, or exported and also in a
        # database passed separately, is one task, not two.
        if not isinstance(session_id, str) or session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        origin = doc.get("origin", "export")
        summary.origins[origin] = summary.origins.get(origin, 0) + 1
        records.extend(_convert_session(doc, summary, pricing_context))
    summary.total_records = len(records)
    return records, summary


def _sorted_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(m: dict[str, Any]) -> tuple[int, str]:
        created = (m["info"].get("time") or {}).get("created")
        return (created if isinstance(created, (int, float)) else 0, str(m["info"].get("id", "")))

    out = []
    for m in sorted(messages, key=key):
        parts = sorted(
            (p for p in m.get("parts") or [] if isinstance(p, dict)),
            key=lambda p: str(p.get("id", "")),
        )
        out.append({"info": m["info"], "parts": parts})
    return out


def _split_steps(parts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Each step's parts, from its step-start through its step-finish (and
    the patch part OpenCode appends after it). Parts before the first
    step-start (none, normally) join the first step."""
    steps: list[list[dict[str, Any]]] = []
    lead: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") == "step-start":
            steps.append([part])
        elif steps:
            steps[-1].append(part)
        else:
            lead.append(part)
    if steps:
        steps[0][1:1] = lead
    elif lead:
        steps = [lead]
    return steps


def _convert_session(
    doc: dict[str, Any],
    summary: ConversionSummary,
    pricing_context: pricing.PricingContext,
) -> list[dict[str, Any]]:
    session = doc[_DOC_MARKER]
    task_id = session["id"]
    summary.sessions_total += 1
    session_meta: dict[str, Any] = {"task_id_source": "session_id"}
    if session.get("parentID"):
        summary.subagent_sessions += 1
        session_meta["parent_task_id"] = session["parentID"]
        session_meta["parent_task_link_kind"] = "delegation"

    messages = _sorted_messages(doc.get("messages") or [])
    records: list[dict[str, Any]] = []

    def emit(record: dict[str, Any]) -> None:
        record["step_index"] = len(records)
        record["parent_id"] = len(records) - 1 if records else None
        records.append(record)

    for index, message in enumerate(messages):
        info = message["info"]
        if info.get("role") != "assistant":
            continue
        summary.retry_parts += sum(1 for p in message["parts"] if p.get("type") == "retry")
        workflow = info.get("agent") or _NO_WORKFLOW_LABEL
        workflow_basis = "agent" if info.get("agent") else "no_agent"
        steps = _split_steps(message["parts"]) or [[]]
        for step_number, step in enumerate(steps):
            earlier_parts = [p for s in steps[:step_number] for p in s]
            context = _model_context(messages, index, earlier_parts)
            new_input = (
                _project_parts(steps[step_number - 1]) if step_number
                else _new_input_before(messages, index)
            )
            llm = _llm_record(
                info, step, len(steps) == 1, step_number == len(steps) - 1, context, new_input,
                task_id, workflow, workflow_basis, session_meta, summary, pricing_context,
            )
            emit(llm)
            tools = [p for p in step if p.get("type") == "tool"]
            changed_files = any(
                p.get("type") == "patch" and p.get("files") for p in step
            )
            if changed_files and len(tools) > 1:
                summary.patch_steps_with_several_tools += 1
            for part in tools:
                call, result = _tool_records(
                    part, llm, task_id, workflow, workflow_basis, session_meta, summary,
                    wrote=changed_files and len(tools) == 1,
                )
                emit(call)
                if result is not None:
                    emit(result)
    return records


def _llm_record(
    info: dict[str, Any],
    step: list[dict[str, Any]],
    only_step: bool,
    last_step: bool,
    context: list[Any],
    new_input: list[Any],
    task_id: str,
    workflow: str,
    workflow_basis: str,
    session_meta: dict[str, Any],
    summary: ConversionSummary,
    pricing_context: pricing.PricingContext,
) -> dict[str, Any]:
    summary.llm_calls += 1
    model = info.get("modelID") or None
    finish = next((p for p in step if p.get("type") == "step-finish"), None)
    if finish is not None:
        cost_raw, tokens = finish.get("cost"), finish.get("tokens") or {}
    elif only_step:
        # Rule 1: no step-finish anywhere means the message's own totals
        # are this one call's.
        cost_raw, tokens = info.get("cost"), info.get("tokens") or {}
    else:
        cost_raw, tokens = None, {}
    tokens_in, tokens_out, cache_read, cache_write = _token_counts(tokens)

    redacted = _contains_redaction(context)
    if redacted:
        summary.records_with_redacted_content += 1
        digest, masks = _opaque_hash(task_id, info.get("id"), "llm", step[0].get("id") if step else "")
        basis = "opaque"
    else:
        digest, masks = hashing.content_hash(context, structured=True)
        basis = "prompt"

    metadata: dict[str, Any] = {
        "hash_spec": hashing.HASH_SPEC,
        "content_basis": basis,
        "masked_spans": masks,
        "opencode_message_id": info.get("id"),
        "provider": info.get("providerID"),
        "workflow_basis": workflow_basis,
        **session_meta,
    }
    if not redacted:
        metadata["similarity_fingerprint"] = hashing.similarity_fingerprint(new_input, structured=True)
        metadata["similarity_spec"] = hashing.SIMILARITY_SPEC
        response = {"parts": _project_parts(step), "reason": finish.get("reason") if finish else None}
        metadata["response_hash"], _ = hashing.content_hash(response, structured=True)
    if finish is not None:
        metadata["finish_reason"] = finish.get("reason")

    cost_usd = _opencode_cost(cost_raw, tokens)
    if cost_usd is not None:
        summary.llm_calls_priced_by_opencode += 1
    else:
        estimated = pricing.estimate_cost_usd(
            pricing_context.entries, model,
            tokens_in=_int_or_none(tokens.get("input")), tokens_out=tokens_out,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        )
        if estimated is not None:
            cost_usd = estimated
            metadata["cost_basis"] = "estimated_from_bundled_pricing_table"
            summary.llm_calls_priced_by_bundled_table += 1
            summary.pricing_table_generated_at = pricing_context.generated_at
        else:
            summary.llm_calls_unpriced += 1

    # A finished step succeeded. An unfinished one failed, or was cut off
    # (the message's own error, or no completion time, says which).
    if finish is not None:
        outcome = "ok"
    elif not last_step or info.get("error") is not None:
        outcome = "error"
    else:
        outcome = "ok" if (info.get("time") or {}).get("completed") else "error"
    if outcome == "error" and last_step and isinstance(info.get("error"), dict):
        metadata["error_name"] = info["error"].get("name")

    start = next((p for p in step if p.get("type") == "step-start"), None)
    return {
        "task_id": task_id,
        "step_index": None,
        "event_type": "llm_call",
        "name": model or "unknown",
        "content_hash": digest,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "outcome": outcome,
        "timestamp": _ms_iso(_part_time(start) or (info.get("time") or {}).get("created")),
        "cost_usd": cost_usd,
        "model": model,
        "parent_id": None,
        "workflow": workflow,
        "metadata": metadata,
    }


def _tool_records(
    part: dict[str, Any],
    llm: dict[str, Any],
    task_id: str,
    workflow: str,
    workflow_basis: str,
    session_meta: dict[str, Any],
    summary: ConversionSummary,
    *,
    wrote: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    summary.tool_calls += 1
    state = part.get("state") or {}
    status = state.get("status")
    tool = str(part.get("tool") or "unknown_tool")
    arguments = state.get("input") if state.get("input") is not None else {}
    common = {
        "hash_spec": hashing.HASH_SPEC,
        "tool_call_id": part.get("callID"),
        "opencode_part_id": part.get("id"),
        "model_basis": "issuing_llm_call",
        "workflow_basis": workflow_basis,
        **session_meta,
    }
    times = state.get("time") or {}

    call_meta = dict(common)
    if _is_redacted(arguments):
        summary.records_with_redacted_content += 1
        call_hash, masks = _opaque_hash(task_id, part.get("id"), "call", 0)
        call_meta["content_basis"] = "opaque"
    else:
        call_hash, masks = hashing.content_hash(arguments, structured=True)
        call_meta["content_basis"] = "tool_input"
        call_meta["similarity_fingerprint"] = hashing.similarity_fingerprint(arguments, structured=True)
        call_meta["similarity_spec"] = hashing.SIMILARITY_SPEC
    call_meta["masked_spans"] = masks
    if wrote:
        summary.tool_calls_marked_write += 1
        call_meta["write"] = True
        call_meta["write_basis"] = "step_patch_single_tool"
    if tool == "task" and isinstance(state.get("metadata"), dict):
        child = state["metadata"].get("sessionId") or state["metadata"].get("sessionID")
        if isinstance(child, str):
            call_meta["child_task_id"] = child

    call = {
        "task_id": task_id,
        "step_index": None,
        "event_type": "tool_call",
        "name": tool,
        "content_hash": call_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": None,
        "timestamp": _ms_iso(times.get("start")) or llm["timestamp"],
        "cost_usd": None,
        "model": llm["model"],
        "parent_id": None,
        "workflow": workflow,
        "metadata": call_meta,
    }

    if status not in ("completed", "error"):
        summary.tool_calls_without_result += 1
        return call, None

    output = state.get("output") if status == "completed" else state.get("error")
    result_meta = dict(common)
    if _is_redacted(output):
        summary.records_with_redacted_content += 1
        result_hash, masks = _opaque_hash(task_id, part.get("id"), "result", 0)
        result_meta["content_basis"] = "opaque"
    else:
        result_hash, masks = hashing.content_hash(output if output is not None else "", structured=False)
        result_meta["content_basis"] = "tool_output"
    result_meta["masked_spans"] = masks
    result = {
        "task_id": task_id,
        "step_index": None,
        "event_type": "tool_result",
        "name": tool,
        "content_hash": result_hash,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": "ok" if status == "completed" else "error",
        "timestamp": _ms_iso(times.get("end")) or call["timestamp"],
        "cost_usd": None,
        "model": llm["model"],
        "parent_id": None,
        "workflow": workflow,
        "metadata": result_meta,
    }
    return call, result


# --- context reconstruction (rule 3) ------------------------------------------


def _model_context(
    messages: list[dict[str, Any]], index: int, earlier_parts: list[dict[str, Any]]
) -> list[Any]:
    """What OpenCode would send for a step of messages[index]: the
    messages before it, cut at the latest completed compaction the way
    filterCompacted() cuts, then this message's own earlier steps."""
    before = _after_compaction(messages[:index])
    context = [_project_message(m) for m in before]
    if earlier_parts:
        context.append({"role": "assistant", "parts": _project_parts(earlier_parts)})
    return context


def _after_compaction(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed = {
        m["info"].get("parentID")
        for m in messages
        if m["info"].get("role") == "assistant"
        and m["info"].get("summary")
        and m["info"].get("finish")
        and not m["info"].get("error")
    }
    for i in range(len(messages) - 1, -1, -1):
        info = messages[i]["info"]
        if info.get("role") != "user" or info.get("id") not in completed:
            continue
        compaction = next((p for p in messages[i]["parts"] if p.get("type") == "compaction"), None)
        if compaction is None:
            continue
        summary_index = next(
            (j for j in range(i + 1, len(messages))
             if messages[j]["info"].get("role") == "assistant"
             and messages[j]["info"].get("summary")
             and messages[j]["info"].get("parentID") == info.get("id")),
            None,
        )
        tail_start = compaction.get("tail_start_id")
        tail: list[dict[str, Any]] = []
        if tail_start:
            start = next((k for k in range(i) if messages[k]["info"].get("id") == tail_start), None)
            if start is not None:
                tail = messages[start:i]
        if summary_index is None:
            return messages[i:]
        return [messages[i], messages[summary_index]] + tail + messages[summary_index + 1:]
    return messages


def _new_input_before(messages: list[dict[str, Any]], index: int) -> list[Any]:
    """What's new for a message's first step: the previous assistant
    step's tool calls and results when the turn is still going, else the
    user's message. OpenCode 1.19 stores each step of a turn as its own
    assistant message, so a first step isn't necessarily a turn's first."""
    for m in messages[:index][::-1]:
        if m["info"].get("role") == "assistant":
            steps = _split_steps(m["parts"])
            return _project_parts(steps[-1]) if steps else []
        return [_project_message(m)]
    return []


def _project_message(message: dict[str, Any]) -> dict[str, Any]:
    info = message["info"]
    out: dict[str, Any] = {"role": info.get("role"), "parts": _project_parts(message["parts"])}
    if info.get("role") == "user" and info.get("system"):
        out["system"] = info["system"]
    return out


# Parts that are bookkeeping, not model input.
_SKIPPED_PART_TYPES = frozenset({"step-start", "step-finish", "snapshot", "patch", "retry"})
_VOLATILE_PART_FIELDS = frozenset({
    "id", "sessionID", "messageID", "time", "metadata", "synthetic", "snapshot",
})


def _project_parts(parts: list[dict[str, Any]]) -> list[Any]:
    out = []
    for part in parts:
        kind = part.get("type")
        if kind in _SKIPPED_PART_TYPES or part.get("ignored"):
            continue
        if kind == "tool":
            state = part.get("state") or {}
            out.append({
                "type": "tool",
                "tool": part.get("tool"),
                "input": state.get("input"),
                "status": state.get("status"),
                "output": state.get("output") if state.get("status") == "completed" else state.get("error"),
            })
            continue
        out.append({k: v for k, v in part.items() if k not in _VOLATILE_PART_FIELDS})
    return out


# --- small helpers -----------------------------------------------------------


def _is_redacted(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith(_REDACTED_PREFIX)
    if isinstance(value, dict):
        return set(value) == {"redacted"} and isinstance(value["redacted"], str)
    return False


def _contains_redaction(value: Any) -> bool:
    if _is_redacted(value):
        return True
    if isinstance(value, dict):
        return any(_contains_redaction(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_redaction(v) for v in value)
    return False


def _opaque_hash(task_id: str, owner_id: Any, kind: str, salt: Any) -> tuple[str, int]:
    return hashing.content_hash(f"opencode-opaque:{task_id}:{owner_id}:{kind}:{salt}")


def _token_counts(tokens: dict[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    """tokens_in includes cache reads and writes, matching claude_code.py
    and pi.py. OpenCode's `reasoning` is reported alongside `output`, not
    inside it, so it's added to tokens_out."""
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    input_tokens = _int_or_none(tokens.get("input"))
    output_tokens = _int_or_none(tokens.get("output"))
    reasoning = _int_or_none(tokens.get("reasoning")) or 0
    cache_read = _int_or_none(cache.get("read"))
    cache_write = _int_or_none(cache.get("write"))
    tokens_in = None
    if input_tokens is not None or cache_read or cache_write:
        tokens_in = (input_tokens or 0) + (cache_read or 0) + (cache_write or 0)
    tokens_out = None if output_tokens is None else output_tokens + reasoning
    return tokens_in, tokens_out, cache_read, cache_write


def _opencode_cost(cost: Any, tokens: dict[str, Any]) -> float | None:
    """OpenCode's own cost, unless it's 0 with real tokens: a model OpenCode
    has no price for and a genuinely free one look identical."""
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        return None
    if cost > 0:
        return float(cost)
    tokens_in, tokens_out, _, _ = _token_counts(tokens)
    return 0.0 if not tokens_in and not tokens_out else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _part_time(part: dict[str, Any] | None) -> Any:
    if part is None:
        return None
    time = part.get("time")
    return time.get("start") if isinstance(time, dict) else None


def _ms_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


class OpenCodeSource(AdapterSource):
    name = SOURCE_NAME

    def load(self, path: Path) -> list[dict[str, Any]] | None:
        return load_opencode(path)

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if _DOC_MARKER in doc:
                return Detection(SOURCE_NAME, f"OpenCode session ({doc.get('origin', 'export')})")
        return None

    def convert(self, documents: list[dict[str, Any]]):
        return convert_opencode(documents)
