"""Shared post-pass: fill `model` on tool_call/tool_result records that
have none, for every adapter in this package -- not implemented per
source because the rule is identical everywhere a source distinguishes
llm_call from tool_call/tool_result at all: a tool call has no model of
its own, but the model actively running its workflow is a real,
derivable fact from the surrounding trace, not a guess.

An earlier version of this lived duplicated inside each adapter's own
per-event forward pass (a running "last model seen" dict, updated only
looking backward in time). That missed a real, confirmed shape: a
task's very first kept event can genuinely be a tool_call with no
`llm_call` anywhere before it in that same task and workflow --
confirmed against real Claude Code captures where a session's step 0 is
a `Bash` call, its first `llm_request` only appearing later (step 2+).
A forward-only pass leaves that tool_call's model at None forever, even
though the very next llm_call in the same workflow is right there and
is, in every observed case, the same model that was about to run (or
had just started) when this call happened. Looking both directions and
taking whichever llm_call is nearer fixes this without weakening the
"never guess" discipline: it's still a real fact about this task's own
execution, not an invented default.
"""

from __future__ import annotations

from typing import Any

_CALL_TYPES = ("llm_call", "tool_call", "tool_result")


def fill_missing_tool_model(records: list[dict[str, Any]]) -> None:
    """Mutates `records` in place. Groups by (task_id, workflow) --
    records within one group are assumed already in chronological order
    relative to each other (true for every adapter in this package: each
    builds its whole per-task/per-run record list in one start-time-sorted
    pass) -- and for every tool_call/tool_result with `model` falsy in
    that group, fills it from the nearest llm_call by position, checking
    both backward and forward, preferring the earlier one on a tie (what
    was already running when this happened is a slightly better read
    than what ran right after). `metadata.model_basis` is set to
    `"preceding_llm_call"` or `"following_llm_call"` accordingly.

    A tool event with no llm_call anywhere in its own (task_id, workflow)
    group -- a corpus with no llm_call events at all, e.g. a pure
    tool-only capture -- is left at model=None, with no model_basis key:
    genuinely nothing in this corpus to derive a model from, not a gap in
    this function.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        if r["event_type"] in _CALL_TYPES:
            groups.setdefault((r["task_id"], r["workflow"]), []).append(r)

    for group in groups.values():
        llm_indices = [i for i, r in enumerate(group) if r["event_type"] == "llm_call"]
        if not llm_indices:
            continue
        for i, record in enumerate(group):
            if record["event_type"] == "llm_call" or record["model"]:
                continue
            nearest = min(llm_indices, key=lambda j: (abs(j - i), 0 if j < i else 1))
            model = group[nearest]["model"]
            if not model:
                continue
            record["model"] = model
            record["metadata"]["model_basis"] = (
                "preceding_llm_call" if nearest < i else "following_llm_call"
            )
