"""Pi coding agent session JSONL -> redundo.analyzer's Event schema (as
plain dicts; see docs/pi.md).

Pi (github.com/earendil-works/pi) emits no OpenTelemetry at all. What it
does have is an append-only, documented session file per conversation,
`~/.pi/agent/sessions/--<cwd>--/<timestamp>_<session-id>.jsonl`, so this
is the one built-in source that reads a native capture instead of OTLP,
through `AdapterSource.load()`. Format reference:
packages/coding-agent/docs/session-format.md and message-types.md in
Pi's own repo.

Six rules that aren't obvious from the code:

1. Lineage is Pi's own entry tree, not a guess. Every entry has
   `id`/`parentId`, and `/tree` branching creates real siblings, so two
   branches repeating the same call are fan-out, never flagged. Within
   one assistant turn the records chain linearly, the same shape
   claude_code.py produces: llm_call, then each tool_call immediately
   followed by its own tool_result, in the order Pi recorded the
   results. A tool_call is emitted at its result's position (joined
   exactly by `toolCallId`, no positional matching), so every result of
   a parallel batch stays an ancestor of the next llm_call. A tool_call
   that never got a result (an aborted turn) is emitted right after its
   llm_call, as a leaf.
2. An llm_call's content_hash is the model-visible context Pi would have
   sent for that call, rebuilt the way Pi's own buildSessionContext()
   does: the path from the root, the latest compaction on it
   (checkpoint system message, summary, then entries from
   firstKeptEntryId), and the latest context_edit per target (null
   omits, non-null replaces content). This matters for retries: Pi keeps
   a failed attempt in raw history and hides it from later context with
   a context_edit, so an auto-retry hashes identical to the attempt it
   repeats, as it should. Volatile per-message fields (timestamps, usage,
   provider signatures) are dropped before hashing. The similarity
   fingerprint covers only what's new in the request (the messages since
   the last assistant reply), since every context contains the one
   before it and would otherwise look similar to it.
3. `/fork` and `/clone` copy the source session's entries into the new
   file with their original ids and timestamps (session-manager.ts,
   forkFrom() and createBranchedSession()). Emitting those copies would
   report the same calls twice, in two tasks. A session whose header has
   `parentSession` skips every entry timestamped before its own header
   (the fork moment). The copies still feed the context rebuild in rule
   2, since the model did see them. The fork is linked back to its
   parent as metadata.parent_task_id with
   parent_task_link_kind="continuation", the first source to supply a
   real continuation link.
4. Cost is Pi's own: every assistant message carries usage.cost.total,
   computed by Pi from its model registry, used as-is. A zero total with
   nonzero tokens means Pi had no price for that model (a local or
   custom provider can also genuinely cost zero, and the two look
   identical), so that call falls back to the bundled pricing table and
   otherwise stays unpriced rather than reporting a real-looking $0.
5. Spend with no call attached (`usage` entries such as cache warming,
   compaction and branch-summary generation, a tool's nested model work
   on toolResult.usage) becomes a synthesized_cost_only llm_call record,
   kept for dollars and never a repeat candidate.
6. A tool call's model is the model of the assistant message that issued
   it, known exactly here (metadata.model_basis="issuing_llm_call"),
   instead of the nearest-llm_call inference other sources need. Pi has
   no subagents of its own, so workflow is always "main". No write flag
   is set: Pi's tool results don't say whether a tool mutated anything,
   and this package never infers that from a tool's name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import hashing, pricing
from ..base import AdapterSource, Detection, InputLoadError

SOURCE_NAME = "pi"
_DOC_MARKER = "pi_session_file"
_SUPPORTED_VERSIONS = frozenset({1, 2, 3})
_NO_WORKFLOW_LABEL = "main"
# Roles Pi converts into model context (message-types.md). bashExecution
# becomes user text unless excludeFromContext; custom becomes a user
# message. system messages are handled separately (compaction folds them).
_CONTEXT_ROLES = frozenset({"user", "assistant", "toolResult", "bashExecution", "custom"})
# Entry types whose own `usage` is real spend with no call record of its own.
_USAGE_BEARING_ENTRY_TYPES = frozenset({"usage", "compaction", "branch_summary"})
_ERROR_STOP_REASONS = frozenset({"error", "aborted"})


@dataclass
class ConversionSummary:
    sessions_total: int = 0
    forked_sessions: int = 0
    forked_sessions_with_unresolved_parent: int = 0
    copied_entries_skipped: int = 0
    total_records: int = 0
    llm_calls: int = 0
    llm_calls_priced_by_pi: int = 0
    llm_calls_priced_by_bundled_table: int = 0
    llm_calls_unpriced: int = 0
    tool_calls: int = 0
    tool_calls_without_result: int = 0
    records_synthesized_from_usage_only: int = 0
    cost_usd_synthesized_from_usage_only: float = 0.0
    entries_by_unhandled_type: dict[str, int] = field(default_factory=dict)
    pricing_table_generated_at: str | None = None

    @property
    def skipped_by_kind(self) -> dict[str, int]:
        return self.entries_by_unhandled_type

    def notes(self) -> list[str]:
        out = []
        if self.total_records == 0:
            out.append("no records produced -- nothing below is meaningful.")
            return out
        out.append(
            f"{self.sessions_total} Pi session(s): {self.llm_calls} llm_call(s), "
            f"{self.tool_calls} tool_call(s)."
        )
        if self.llm_calls:
            out.append(
                f"{self.llm_calls_priced_by_pi}/{self.llm_calls} llm_call(s) priced by Pi "
                f"itself, {self.llm_calls_priced_by_bundled_table} estimated from the "
                f"bundled pricing table (Pi reported $0 with nonzero tokens), "
                f"{self.llm_calls_unpriced} unpriced."
            )
        if self.forked_sessions:
            out.append(
                f"{self.forked_sessions} session(s) were forked or cloned from another "
                f"session; {self.copied_entries_skipped} copied entries skipped so "
                "the same calls aren't counted twice (see docs/pi.md)."
            )
        if self.forked_sessions_with_unresolved_parent:
            out.append(
                f"{self.forked_sessions_with_unresolved_parent} forked session(s) name a "
                "parent session file that wasn't loaded and couldn't be read, so they "
                "carry no parent_task_id link."
            )
        if self.tool_calls_without_result:
            out.append(
                f"{self.tool_calls_without_result} tool_call(s) have no recorded result "
                "(an aborted turn) and no tool_result record."
            )
        if self.records_synthesized_from_usage_only:
            out.append(
                f"{self.records_synthesized_from_usage_only} llm_call record(s) "
                "synthesized from usage with no call of its own (cache warming, "
                "compaction or branch summaries, nested tool model work): "
                f"{self.cost_usd_synthesized_from_usage_only:.4f} USD."
            )
        if self.pricing_table_generated_at:
            note = pricing.pricing_staleness_note(self.pricing_table_generated_at)
            if note:
                out.append(note)
        return out


# --- loading -----------------------------------------------------------------


def load_pi_sessions(path: Path) -> list[dict[str, Any]] | None:
    """A single session file, or a directory whose top-level *.jsonl files
    include Pi sessions (non-recursive on purpose: point this at one
    project's session directory, not all of ~/.pi). None when nothing at
    `path` is a Pi session. Files that aren't Pi sessions are ignored.
    """
    if path.is_file():
        candidates = [path] if path.suffix == ".jsonl" else []
    elif path.is_dir():
        candidates = sorted(path.glob("*.jsonl"))
    else:
        return None

    documents = []
    for file in candidates:
        header = _read_header(file)
        if header is None:
            continue
        documents.append({
            _DOC_MARKER: str(file.resolve()),
            "header": header,
            "entries": _read_entries(file),
        })
    if not documents:
        return None

    id_by_path = {doc[_DOC_MARKER]: doc["header"]["id"] for doc in documents}
    for doc in documents:
        parent = doc["header"].get("parentSession")
        if isinstance(parent, str) and parent:
            doc["parent_session_id"] = _resolve_parent_session_id(parent, id_by_path)
    return documents


def _read_header(file: Path) -> dict[str, Any] | None:
    try:
        with file.open(encoding="utf-8") as handle:
            first = handle.readline()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        header = json.loads(first)
    except json.JSONDecodeError:
        return None
    if (
        isinstance(header, dict)
        and header.get("type") == "session"
        and isinstance(header.get("id"), str)
        and header.get("version", 1) in _SUPPORTED_VERSIONS
    ):
        return header
    return None


def _read_entries(file: Path) -> list[dict[str, Any]]:
    entries = []
    try:
        lines = file.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputLoadError(f"{file}: could not read Pi session file ({type(exc).__name__})") from None
    for line_no, raw in enumerate(lines[1:], start=2):
        raw = raw.rstrip("\r")
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            # Pi appends one line at a time; a session still being written
            # can end on a partial line. Anything malformed before that is
            # a real problem worth stopping for.
            if line_no == len(lines) or (line_no == len(lines) - 1 and not lines[-1].strip()):
                continue
            raise InputLoadError(f"{file}:{line_no}: not valid JSON") from None
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _resolve_parent_session_id(parent_path: str, id_by_path: dict[str, str]) -> str | None:
    """The parent's session id: from the loaded set when it's there, else
    from the parent file's header line alone (never its content)."""
    try:
        resolved = str(Path(parent_path).expanduser().resolve())
    except (OSError, RuntimeError):
        return None
    if resolved in id_by_path:
        return id_by_path[resolved]
    header = _read_header(Path(resolved))
    return header["id"] if header else None


# --- conversion --------------------------------------------------------------


def convert_pi(
    documents: list[dict[str, Any]],
    *,
    pricing_context: pricing.PricingContext | None = None,
) -> tuple[list[dict[str, Any]], ConversionSummary]:
    summary = ConversionSummary()
    if pricing_context is None:
        pricing_context = pricing.load_pricing_context()
    records: list[dict[str, Any]] = []
    for doc in documents:
        if _DOC_MARKER not in doc:
            continue
        records.extend(_convert_session(doc, summary, pricing_context))
    summary.total_records = len(records)
    return records, summary


def _normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Version 1 sessions (legacy, linear) have no id/parentId; give each
    entry a positional id chained to the one before, which is exactly the
    linear thread v1 represents."""
    out = []
    previous_id = None
    for index, entry in enumerate(entries):
        if "id" not in entry:
            entry = {**entry, "id": f"#{index}", "parentId": previous_id}
        out.append(entry)
        previous_id = entry["id"]
    return out


def _convert_session(
    doc: dict[str, Any],
    summary: ConversionSummary,
    pricing_context: pricing.PricingContext,
) -> list[dict[str, Any]]:
    header = doc["header"]
    task_id = header["id"]
    entries = _normalize_entries(doc["entries"])
    by_id = {e["id"]: e for e in entries if isinstance(e.get("id"), str)}
    summary.sessions_total += 1

    fork_cutoff = None
    parent_task_id = None
    if header.get("parentSession"):
        summary.forked_sessions += 1
        fork_cutoff = _parse_iso(header.get("timestamp"))
        parent_task_id = doc.get("parent_session_id")
        if parent_task_id is None:
            summary.forked_sessions_with_unresolved_parent += 1

    session_meta: dict[str, Any] = {"task_id_source": "session_id"}
    if parent_task_id is not None:
        session_meta["parent_task_id"] = parent_task_id
        session_meta["parent_task_link_kind"] = "continuation"

    # toolCallIds that got a result anywhere in this file, so a call with
    # none can be emitted as a leaf right after its llm_call (rule 1).
    resulted_call_ids = {
        e["message"].get("toolCallId")
        for e in entries
        if e.get("type") == "message"
        and isinstance(e.get("message"), dict)
        and e["message"].get("role") == "toolResult"
    }

    records: list[dict[str, Any]] = []
    step_of_entry: dict[str, int] = {}
    # toolCallId -> (block, issuing llm_call record) for calls awaiting a result
    pending_calls: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def parent_step(entry: dict[str, Any]) -> int | None:
        seen = set()
        current = entry.get("parentId")
        while isinstance(current, str) and current not in seen:
            seen.add(current)
            if current in step_of_entry:
                return step_of_entry[current]
            ancestor = by_id.get(current)
            if ancestor is None:
                return None
            current = ancestor.get("parentId")
        return None

    def emit(record: dict[str, Any]) -> int:
        record["step_index"] = len(records)
        records.append(record)
        return record["step_index"]

    for entry in entries:
        copied = fork_cutoff is not None and _is_before(entry.get("timestamp"), fork_cutoff)
        if copied:
            summary.copied_entries_skipped += 1
            continue

        entry_type = entry.get("type")
        message = entry.get("message") if entry_type == "message" else None
        role = message.get("role") if isinstance(message, dict) else None

        if role == "assistant":
            if message.get("stopReason") == "pending":
                continue
            llm = _llm_record(entry, message, by_id, task_id, session_meta, summary, pricing_context)
            llm["parent_id"] = parent_step(entry)
            llm_step = emit(llm)
            step_of_entry[entry["id"]] = llm_step
            for block in message.get("content") or []:
                if not (isinstance(block, dict) and block.get("type") == "toolCall"):
                    continue
                summary.tool_calls += 1
                call_id = block.get("id")
                if call_id in resulted_call_ids:
                    pending_calls[call_id] = (block, llm)
                else:
                    summary.tool_calls_without_result += 1
                    call = _tool_call_record(block, entry, llm, task_id, session_meta)
                    call["parent_id"] = llm_step
                    emit(call)

        elif role == "toolResult":
            call_id = message.get("toolCallId")
            pending = pending_calls.pop(call_id, None)
            prev = parent_step(entry)
            if pending is not None:
                block, llm = pending
                call = _tool_call_record(block, entry, llm, task_id, session_meta)
                call["parent_id"] = prev
                call_step = emit(call)
                model, name = llm["model"], call["name"]
            else:
                # A result whose call lies before a fork point (or was never
                # recorded): no tool_call record to attach to.
                call_step = prev
                model, name = None, _tool_name(message.get("toolName"), None)
            result = _tool_result_record(entry, message, name, model, task_id, session_meta)
            result["parent_id"] = call_step
            step_of_entry[entry["id"]] = emit(result)
            if isinstance(message.get("usage"), dict):
                synthesized = _usage_only_record(
                    entry, message["usage"], "tool_nested", None, task_id, session_meta, summary
                )
                if synthesized is not None:
                    emit(synthesized)

        elif entry_type in _USAGE_BEARING_ENTRY_TYPES:
            usage = entry.get("usage")
            if isinstance(usage, dict):
                kind = entry.get("kind") if entry_type == "usage" else entry_type
                synthesized = _usage_only_record(
                    entry, usage, str(kind or "usage"), entry.get("model"),
                    task_id, session_meta, summary,
                )
                if synthesized is not None:
                    emit(synthesized)

        elif role is None and entry_type not in _KNOWN_NON_CALL_ENTRY_TYPES:
            key = str(entry_type)
            summary.entries_by_unhandled_type[key] = summary.entries_by_unhandled_type.get(key, 0) + 1

    return records


_KNOWN_NON_CALL_ENTRY_TYPES = frozenset({
    "message", "model_change", "thinking_level_change", "context_edit",
    "custom", "custom_message", "label", "session_info",
})


def _llm_record(
    entry: dict[str, Any],
    message: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    task_id: str,
    session_meta: dict[str, Any],
    summary: ConversionSummary,
    pricing_context: pricing.PricingContext,
) -> dict[str, Any]:
    summary.llm_calls += 1
    model = message.get("model") or None
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    tokens_in, tokens_out, cache_read, cache_write = _token_counts(usage)

    context = _model_context(entry, by_id)
    digest, masks = hashing.content_hash(context, structured=True)
    # Similarity is judged on what's new in this request, not the whole
    # context: every call's context contains the previous call's, so a
    # whole-context fingerprint calls every consecutive pair similar.
    fingerprint = hashing.similarity_fingerprint(_new_input(context), structured=True)
    response = {
        "content": [_strip_block(b) for b in message.get("content") or []],
        "stopReason": message.get("stopReason"),
        "errorMessage": message.get("errorMessage"),
    }
    response_hash, _ = hashing.content_hash(response, structured=True)

    metadata: dict[str, Any] = {
        "hash_spec": hashing.HASH_SPEC,
        "content_basis": "prompt",
        "masked_spans": masks,
        "similarity_fingerprint": fingerprint,
        "similarity_spec": hashing.SIMILARITY_SPEC,
        "response_hash": response_hash,
        "pi_entry_id": entry.get("id"),
        "provider": message.get("provider"),
        "stop_reason": message.get("stopReason"),
        "workflow_basis": "no_delegation",
        **session_meta,
    }
    if message.get("responseModel") and message.get("responseModel") != model:
        metadata["response_model"] = message["responseModel"]

    cost_usd = _pi_cost(usage)
    if cost_usd is not None:
        summary.llm_calls_priced_by_pi += 1
    else:
        estimated = pricing.estimate_cost_usd(
            pricing_context.entries, model,
            tokens_in=_int_or_none(usage.get("input")), tokens_out=tokens_out,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        )
        if estimated is not None:
            cost_usd = estimated
            metadata["cost_basis"] = "estimated_from_bundled_pricing_table"
            summary.llm_calls_priced_by_bundled_table += 1
            summary.pricing_table_generated_at = pricing_context.generated_at
        else:
            summary.llm_calls_unpriced += 1

    return {
        "task_id": task_id,
        "step_index": None,
        "event_type": "llm_call",
        "name": model or "unknown",
        "content_hash": digest,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "outcome": "error" if message.get("stopReason") in _ERROR_STOP_REASONS else "ok",
        "timestamp": _timestamp(entry, message),
        "cost_usd": cost_usd,
        "model": model,
        "parent_id": None,
        "workflow": _NO_WORKFLOW_LABEL,
        "metadata": metadata,
    }


def _tool_call_record(
    block: dict[str, Any],
    entry: dict[str, Any],
    llm: dict[str, Any],
    task_id: str,
    session_meta: dict[str, Any],
) -> dict[str, Any]:
    arguments = block.get("arguments")
    if arguments is None:
        arguments = {}
    digest, masks = hashing.content_hash(arguments, structured=True)
    return {
        "task_id": task_id,
        "step_index": None,
        "event_type": "tool_call",
        "name": _tool_name(block.get("name"), block.get("namespace")),
        "content_hash": digest,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": None,
        # When the model asked for it, not when the result landed.
        "timestamp": llm["timestamp"],
        "cost_usd": None,
        "model": llm["model"],
        "parent_id": None,
        "workflow": _NO_WORKFLOW_LABEL,
        "metadata": {
            "hash_spec": hashing.HASH_SPEC,
            "content_basis": "tool_input",
            "masked_spans": masks,
            "similarity_fingerprint": hashing.similarity_fingerprint(arguments, structured=True),
            "similarity_spec": hashing.SIMILARITY_SPEC,
            "tool_call_id": block.get("id"),
            "pi_entry_id": llm["metadata"]["pi_entry_id"],
            "model_basis": "issuing_llm_call",
            "workflow_basis": "no_delegation",
            **session_meta,
        },
    }


def _tool_result_record(
    entry: dict[str, Any],
    message: dict[str, Any],
    name: str,
    model: str | None,
    task_id: str,
    session_meta: dict[str, Any],
) -> dict[str, Any]:
    content = message.get("content")
    digest, masks = hashing.content_hash(content if content is not None else [], structured=True)
    metadata: dict[str, Any] = {
        "hash_spec": hashing.HASH_SPEC,
        "content_basis": "tool_output",
        "masked_spans": masks,
        "tool_call_id": message.get("toolCallId"),
        "pi_entry_id": entry.get("id"),
        "workflow_basis": "no_delegation",
        **session_meta,
    }
    if model is not None:
        metadata["model_basis"] = "issuing_llm_call"
    return {
        "task_id": task_id,
        "step_index": None,
        "event_type": "tool_result",
        "name": name,
        "content_hash": digest,
        "tokens_in": None,
        "tokens_out": None,
        "outcome": "error" if message.get("isError") else "ok",
        "timestamp": _timestamp(entry, message),
        "cost_usd": None,
        "model": model,
        "parent_id": None,
        "workflow": _NO_WORKFLOW_LABEL,
        "metadata": metadata,
    }


def _usage_only_record(
    entry: dict[str, Any],
    usage: dict[str, Any],
    kind: str,
    model: str | None,
    task_id: str,
    session_meta: dict[str, Any],
    summary: ConversionSummary,
) -> dict[str, Any] | None:
    cost_usd = _pi_cost(usage)
    tokens_in, tokens_out, _, _ = _token_counts(usage)
    if cost_usd is None and not tokens_in and not tokens_out:
        return None
    summary.records_synthesized_from_usage_only += 1
    summary.cost_usd_synthesized_from_usage_only += cost_usd or 0.0
    # Unique per entry by construction: never a repeat candidate.
    digest, _ = hashing.content_hash(f"pi-usage:{task_id}:{entry.get('id')}:{kind}")
    return {
        "task_id": task_id,
        "step_index": None,
        "event_type": "llm_call",
        "name": model or f"pi:{kind}",
        "content_hash": digest,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "outcome": None,
        "timestamp": _timestamp(entry, None),
        "cost_usd": cost_usd,
        "model": model,
        "parent_id": None,
        "workflow": _NO_WORKFLOW_LABEL,
        "metadata": {
            "hash_spec": hashing.HASH_SPEC,
            "content_basis": "opaque",
            "synthesized_cost_only": True,
            "usage_kind": kind,
            "pi_entry_id": entry.get("id"),
            "workflow_basis": "no_delegation",
            **session_meta,
        },
    }


# --- context reconstruction (rule 2) ------------------------------------------


def _model_context(entry: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[Any]:
    """What Pi would have sent for the call `entry` answers: its ancestor
    path (not including itself), with compaction and context edits
    applied. A close mirror of Pi's buildSessionContext(), sufficient for
    "was this the same request", not a byte-exact replay of the wire
    payload (system prompt sections aren't merged, just listed in order).
    """
    path = []
    seen = set()
    current = entry.get("parentId")
    while isinstance(current, str) and current not in seen and current in by_id:
        seen.add(current)
        path.append(by_id[current])
        current = by_id[current].get("parentId")
    path.reverse()

    compaction_index = None
    for i, e in enumerate(path):
        if e.get("type") == "compaction":
            compaction_index = i
    selected: list[dict[str, Any]]
    prefix: list[Any] = []
    if compaction_index is not None:
        compaction = path[compaction_index]
        if isinstance(compaction.get("systemMessage"), dict):
            prefix.append(_context_message(compaction["systemMessage"]))
        prefix.append({"role": "compactionSummary", "summary": compaction.get("summary")})
        first_kept = compaction.get("firstKeptEntryId")
        kept_start = next(
            (i for i, e in enumerate(path[:compaction_index]) if e.get("id") == first_kept),
            compaction_index,
        )
        kept = [
            e for e in path[kept_start:compaction_index]
            if not _is_system_message(e)
            or not isinstance(compaction.get("systemMessage"), dict)
        ]
        selected = kept + path[compaction_index + 1:]
    else:
        selected = path

    edits: dict[str, Any] = {}
    for e in path:
        if e.get("type") == "context_edit" and isinstance(e.get("targetId"), str):
            edits[e["targetId"]] = e.get("replacement")

    context = list(prefix)
    for e in selected:
        message = _entry_context_message(e)
        if message is None:
            continue
        if e.get("id") in edits:
            replacement = edits[e["id"]]
            if replacement is None:
                continue
            message = {**message, "content": replacement}
        context.append(message)
    return context


def _new_input(context: list[Any]) -> list[Any]:
    """The messages after the last assistant message in `context` (the
    user turn or tool results this call responds to), system messages
    left out."""
    last_assistant = max(
        (i for i, m in enumerate(context) if isinstance(m, dict) and m.get("role") == "assistant"),
        default=-1,
    )
    return [
        m for m in context[last_assistant + 1:]
        if not (isinstance(m, dict) and m.get("role") == "system")
    ]


def _is_system_message(entry: dict[str, Any]) -> bool:
    message = entry.get("message")
    return entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "system"


def _entry_context_message(entry: dict[str, Any]) -> Any | None:
    entry_type = entry.get("type")
    if entry_type == "message" and isinstance(entry.get("message"), dict):
        message = entry["message"]
        role = message.get("role")
        if role == "system" or role in _CONTEXT_ROLES:
            if role == "bashExecution" and message.get("excludeFromContext"):
                return None
            if role == "assistant" and message.get("stopReason") == "pending":
                return None
            return _context_message(message)
        return None
    if entry_type == "branch_summary":
        return {"role": "branchSummary", "summary": entry.get("summary")}
    if entry_type == "custom_message":
        return {"role": "custom", "content": entry.get("content")}
    return None


# Fields that describe how or when a message was produced, not what the
# model was shown. Keeping them would make every context unique.
_VOLATILE_MESSAGE_FIELDS = frozenset({
    "timestamp", "usage", "api", "provider", "model", "responseModel", "responseId",
    "providerThinkingLevel", "thinkingLevel", "diagnostics", "stopReason", "rawStopReason",
    "endTurn", "errorMessage", "deferred", "details", "nestedCalls", "display",
    "fullOutputPath", "truncated",
})
_VOLATILE_BLOCK_FIELDS = frozenset({"textSignature", "thinkingSignature", "thoughtSignature", "id"})


def _context_message(message: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in message.items() if k not in _VOLATILE_MESSAGE_FIELDS}
    # toolCallId is provider-generated and unique per call; the result's
    # position right after its call already says which call it answers.
    out.pop("toolCallId", None)
    if isinstance(out.get("content"), list):
        out["content"] = [_strip_block(b) for b in out["content"]]
    return out


def _strip_block(block: Any) -> Any:
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if k not in _VOLATILE_BLOCK_FIELDS}
    return block


# --- small helpers -----------------------------------------------------------


def _tool_name(name: Any, namespace: Any) -> str:
    name = str(name) if name else "unknown_tool"
    return f"{namespace}.{name}" if namespace else name


def _token_counts(usage: dict[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    """tokens_in includes cache reads and writes, matching claude_code.py:
    it's what the model actually consumed, cached or not."""
    input_tokens = _int_or_none(usage.get("input"))
    output_tokens = _int_or_none(usage.get("output"))
    cache_read = _int_or_none(usage.get("cacheRead"))
    cache_write = _int_or_none(usage.get("cacheWrite"))
    tokens_in = None
    if input_tokens is not None or cache_read or cache_write:
        tokens_in = (input_tokens or 0) + (cache_read or 0) + (cache_write or 0)
    return tokens_in, output_tokens, cache_read, cache_write


def _pi_cost(usage: dict[str, Any]) -> float | None:
    """Pi's own cost.total, unless it's 0 with real tokens (rule 4)."""
    cost = usage.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return None
    if total > 0:
        return float(total)
    tokens = _int_or_none(usage.get("totalTokens")) or 0
    return 0.0 if tokens == 0 else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_before(value: Any, cutoff: datetime) -> bool:
    parsed = _parse_iso(value)
    return parsed is not None and parsed < cutoff


def _timestamp(entry: dict[str, Any], message: dict[str, Any] | None) -> str | None:
    if message is not None and isinstance(message.get("timestamp"), (int, float)):
        return datetime.fromtimestamp(message["timestamp"] / 1000, tz=timezone.utc).isoformat()
    parsed = _parse_iso(entry.get("timestamp"))
    return parsed.isoformat() if parsed else None


class PiSource(AdapterSource):
    name = SOURCE_NAME

    def load(self, path: Path) -> list[dict[str, Any]] | None:
        return load_pi_sessions(path)

    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        for doc in documents:
            if _DOC_MARKER in doc:
                version = doc["header"].get("version", 1)
                return Detection(SOURCE_NAME, f"Pi session header (version {version})")
        return None

    def convert(self, documents: list[dict[str, Any]]):
        return convert_pi(documents)
