"""The event schema: the contract every trace source normalizes into.

One row per LLM call, tool call, or tool result. Nothing here is
provider-specific -- adapters for MLflow, a bespoke harness, whatever else,
live outside this package and produce this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EventType = Literal["llm_call", "tool_call", "tool_result"]
Outcome = Literal["ok", "error"]

EVENT_TYPES: frozenset[str] = frozenset({"llm_call", "tool_call", "tool_result"})
OUTCOMES: frozenset[str] = frozenset({"ok", "error"})

# Convention for the `metadata` escape hatch. Documented, not enforced --
# a source that doesn't populate these keys just yields "unknown" signals,
# never a wrong guess. See classify.py.
META_WRITE_KEY = "write"  # bool: did this step have a side effect (mutate state)?
META_RESPONSE_HASH_KEY = "response_hash"  # str: hash of an llm_call's completion, if known
# str: SimHash fingerprint of the call's own prompt/arguments (never its
# result), from redundo.adapter.hashing.similarity_fingerprint. Absent
# means "this source doesn't populate it," same convention as every other
# metadata key -- never treated as "definitely no near-duplicate."
META_SIMILARITY_FINGERPRINT_KEY = "similarity_fingerprint"
META_SIMILARITY_SPEC_KEY = "similarity_spec"  # str: version of the fingerprint procedure used
# bool: this record was synthesized from cost-bearing telemetry alone (a
# billing/usage signal with no matching span or trace event at all), real
# spend that would otherwise be silently invisible to every dollar figure
# in a report. Never a candidate for repeat detection: no comparable
# content, no real position in any lineage. Absent or False means "not
# this," same convention as every other metadata key.
META_SYNTHESIZED_COST_ONLY_KEY = "synthesized_cost_only"
# str: which real signal produced this event's cost_usd. Absent means the
# source reported cost_usd directly, as-is (the "direct_from_source"
# canonical label compute_generic_coverage falls back to; no adapter
# actually writes that string, absence itself is the signal). When an
# adapter estimates or apportions cost_usd instead of reading it
# directly, it sets this explicitly, never silently. Known values in
# use today: "estimated_from_bundled_pricing_table" (OpenInference,
# openclaw-localtrace; see docs/pricing.md), "apportioned_from_metrics_by_tokens"
# / "apportioned_from_metrics_equal_split" (OpenClaw; see docs/openclaw.md).
# A record with META_SYNTHESIZED_COST_ONLY_KEY set is reported under its
# own canonical "synthesized_billing_only" basis regardless of this key,
# since that's a stronger, more specific statement about the record's
# origin. This is what lets a report state its own cost figure's
# composition instead of implying every dollar is equally authoritative.
META_COST_BASIS_KEY = "cost_basis"
# str: the task_id of another task this one was delegated from (a real
# subagent/handoff relationship), when a source can confirm one. Never
# inferred from timing, content similarity, or any other guess, only
# ever a value the source itself reported (e.g. hermes-otel's
# hermes.subagent.parent_session_id). Absent means "this source doesn't
# expose one," not "there is no such relationship."
META_PARENT_TASK_ID_KEY = "parent_task_id"
# str: "continuation" or "delegation", which kind of relationship
# metadata.parent_task_id represents. "continuation" means this task is
# a later chapter of the SAME logical thread of work (a session resumed
# or rolled over, e.g. OpenClaw's own internal previousSessionId, not
# yet exposed by any source today). "delegation" means this task was
# spawned BY the parent as a distinct sub-agent, not a continuation of
# it (e.g. hermes-otel's hermes.subagent.parent_session_id, the only
# real parent_task_id source populates today). Absent means the source
# hasn't said; treated as "not continuation" everywhere that distinction
# matters (see context_drift.py), never guessed either way. The two
# relationships need different treatment: walking a delegation edge and
# calling the distance "drift" would compare an orchestrator to a
# subagent it spawned, which was never one continuous thread to begin
# with.
META_PARENT_TASK_LINK_KIND_KEY = "parent_task_link_kind"
# str: where this record's content_hash actually came from -- adapter-
# specific values (e.g. "prompt", "tool_input", "opaque",
# "log_only_no_span"), documented per-source, not enumerated here. The
# one value the analyzer itself depends on is "opaque": a content_hash
# derived from the record's own span/call id rather than real content
# (unique by construction, so it never coincidentally equals another
# record's hash -- see each adapter's own opaque-hash helper). That
# uniqueness makes it safe for candidate-pair generation (two different
# opaque hashes simply never match, a null result, not a wrong one) but
# unsafe for *result-identity* comparison specifically: comparing two
# different opaque hashes to each other would read as "the result
# changed" -- an active, false claim, not an absence of one. See
# classify.py's _correlated_result_hash(), the one place this key is
# actually read.
META_CONTENT_BASIS_KEY = "content_basis"
META_CONTENT_BASIS_OPAQUE = "opaque"
# str: the local filesystem directory `redundo adapt` read this record's
# source OTLP documents from, absolute, resolved at conversion time.
# Stamped once, corpus-wide, by the `redundo adapt` CLI itself after a
# source's own convert() returns -- no adapter sets this, so it's the
# same for every record produced by one `redundo adapt` invocation.
# Absent for NDJSON built by hand, by a script calling an adapter
# directly, or by any other path that skips the CLI. A local, possibly
# identifying path: it's meant to help the *same* user find the raw
# capture behind a specific sample case again, not for redistribution --
# a report carrying this key discloses local filesystem structure
# (usernames in home-directory paths, project layout) if shared outside
# the machine that produced it, same as any other locally-generated file.
META_SOURCE_PATH_KEY = "source_path"


class SchemaError(ValueError):
    """A row doesn't satisfy the contract."""


@dataclass(frozen=True, slots=True)
class Event:
    """One normalized trace event.

    Field-by-field rationale lives in the schema this class encodes:

    task_id       session/conversation/trace grouping key
    step_index    ordering within task
    event_type    llm_call | tool_call | tool_result
    name          model or tool name
    content_hash  hash of prompt or arguments -- never raw content
    tokens_in     input tokens, if the source has them
    tokens_out    output tokens, if the source has them
    outcome       ok | error | None
    timestamp     when the event happened
    cost_usd      dollar-denominated cost, if the source has it
    model         cost fallback + segmentation when cost_usd is absent
    parent_id     the step_index (within this task_id) of the event that
                  produced/spawned this one; None if the source doesn't
                  track branching (see lineage.py for what that implies)
    workflow      free-text segmentation label (agent name, pipeline stage, ...)
    metadata      escape hatch: anything else, keyed by convention (see above)
    """

    task_id: str
    step_index: int
    event_type: EventType
    name: str
    content_hash: str
    tokens_in: int | None
    tokens_out: int | None
    outcome: Outcome | None
    timestamp: datetime | None
    cost_usd: float | None
    model: str | None
    parent_id: int | None
    workflow: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> tuple[str, int]:
        """Canonical identity: (task_id, step_index). What parent_id points into."""
        return (self.task_id, self.step_index)

    @classmethod
    def from_dict(cls, row: dict[str, Any], *, line_no: int | None = None) -> Event:
        """Validate and coerce one raw record. Raises SchemaError on anything
        that isn't recoverable -- missing/malformed required fields. Optional
        fields coerce permissively: absent or empty means None, not a guess.
        """
        where = f" (line {line_no})" if line_no is not None else ""

        def require(key: str) -> Any:
            if key not in row or row[key] in (None, ""):
                raise SchemaError(f"missing required field '{key}'{where}")
            return row[key]

        task_id = str(require("task_id"))

        try:
            step_index = int(require("step_index"))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"step_index must be an int{where}") from exc

        event_type = str(require("event_type"))
        if event_type not in EVENT_TYPES:
            raise SchemaError(
                f"event_type must be one of {sorted(EVENT_TYPES)}, got {event_type!r}{where}"
            )

        name = str(require("name"))
        content_hash = str(require("content_hash"))

        tokens_in = _optional_int(row.get("tokens_in"))
        tokens_out = _optional_int(row.get("tokens_out"))

        outcome_raw = row.get("outcome")
        outcome: Outcome | None = None
        if outcome_raw not in (None, ""):
            if outcome_raw not in OUTCOMES:
                raise SchemaError(
                    f"outcome must be one of {sorted(OUTCOMES)} or empty, got {outcome_raw!r}{where}"
                )
            outcome = outcome_raw

        timestamp = _optional_timestamp(row.get("timestamp"))
        cost_usd = _optional_float(row.get("cost_usd"))
        model = _optional_str(row.get("model"))
        parent_id = _optional_int(row.get("parent_id"))
        workflow = _optional_str(row.get("workflow"))
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise SchemaError(f"metadata must be an object/dict{where}")

        return cls(
            task_id=task_id,
            step_index=step_index,
            event_type=event_type,  # type: ignore[arg-type]
            name=name,
            content_hash=content_hash,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            outcome=outcome,
            timestamp=timestamp,
            cost_usd=cost_usd,
            model=model,
            parent_id=parent_id,
            workflow=workflow,
            metadata=metadata,
        )


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)
