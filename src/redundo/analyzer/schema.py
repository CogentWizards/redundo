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
