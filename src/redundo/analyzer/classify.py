"""Turn a candidate pair into one of three verdicts.

    confirmed_waste    identical args, identical result, no intervening
                        write, task terminated in failure -- all four
                        confirmed, none of them merely assumed.
    likely_legitimate  result changed, OR a write intervened -- either one
                        confirmed is enough, unconditionally. Terminal
                        success is also legit-supporting, but only as a
                        tie-breaker: see below.
    unclassified       everything else: a required signal couldn't be
                        determined from the trace, and no legitimate-use
                        signal fired either -- including a redundant call
                        inside an otherwise-successful task (see below).

Every one of the three underlying signals (result identity, write status,
terminal outcome) has three possible readings, not two: waste-supporting,
legit-supporting, or unknown. Unknown is not "assume the safe default" --
it's a genuine "the trace doesn't say." confirmed_waste requires all three
signals to be positively waste-supporting; if even one is unknown, the
pair falls through to unclassified unless a legit-supporting signal fires
first.

Terminal outcome is task-level, not call-level (see lineage.py): "the task
succeeded" says nothing about whether *this specific* repeated call
contributed to that success. Result identity and write status are
call-level -- they describe the pair itself. When *either* of those is
positively confirmed waste-supporting (identical result, or no intervening
write -- one is enough, they needn't both agree), that call-level evidence
outranks a task-level "it succeeded anyway": the pair falls to unclassified
rather than likely_legitimate, because the trace genuinely doesn't say
whether the repeat mattered, and a whole-task outcome isn't a substitute
for that missing answer. This matters most exactly when the other signal
is unknown, not merely also waste-supporting -- a source with no captured
result (or one with result_hash stripped) must not let terminal success
rush in to fill that gap just because only one call-level signal, not
both, came back confirmed. Terminal success only promotes to
likely_legitimate on its own when *neither* call-level signal is confirmed
waste-supporting -- it's a tie-breaker for genuine unknowns on both sides,
not a signal that can overrule even one confirmed waste-supporting call.
This is deliberate: see the project README for why the unclassified bucket
is not something to be optimized away with heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .cycles import CandidatePair
from .lineage import TaskLineage
from .schema import META_RESPONSE_HASH_KEY, META_WRITE_KEY, Event


class Verdict(str, Enum):
    CONFIRMED_WASTE = "confirmed_waste"
    LIKELY_LEGITIMATE = "likely_legitimate"
    UNCLASSIFIED = "unclassified"


class Signal(str, Enum):
    WASTE_SUPPORTING = "waste_supporting"
    LEGIT_SUPPORTING = "legit_supporting"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Classification:
    pair: CandidatePair
    verdict: Verdict
    result_signal: Signal
    write_signal: Signal
    terminal_signal: Signal
    reason: str  # one-line human-readable explanation, for spot-checking by hand


def classify_pair(pair: CandidatePair, lineage: TaskLineage) -> Classification:
    result_signal, result_note = _result_signal(pair, lineage)
    write_signal, write_note = _write_signal(pair)
    terminal_signal, terminal_note = _terminal_signal(lineage)

    # Call-level signals are unconditional: either one being legit-
    # supporting is enough on its own, regardless of what terminal outcome
    # says about the task as a whole.
    call_level_hits = [
        note
        for signal, note in ((result_signal, result_note), (write_signal, write_note))
        if signal is Signal.LEGIT_SUPPORTING
    ]
    if call_level_hits:
        return Classification(
            pair=pair,
            verdict=Verdict.LIKELY_LEGITIMATE,
            result_signal=result_signal,
            write_signal=write_signal,
            terminal_signal=terminal_signal,
            reason="; ".join(call_level_hits),
        )

    # Either call-level signal alone positively confirming waste is enough
    # to block the terminal-success tie-breaker below -- not just both
    # together. A source with a gap in its data (no captured result, a
    # stripped result_hash) must not let terminal success rush in to fill
    # that gap: "the task succeeded anyway" doesn't speak to whether this
    # specific repeated call contributed, whether the OTHER call-level
    # signal is confirmed waste, confirmed legit (already handled above),
    # or simply unknown. Requiring only one, not both, is what makes a
    # trace with a stripped/missing result_hash degrade to unclassified
    # instead of quietly flipping to a false likely_legitimate -- verified
    # directly against real captured traces with result correlation
    # removed (see analyze_fixture_runs.py --strip-result-hash).
    call_level_any_waste_supporting = (
        result_signal is Signal.WASTE_SUPPORTING or write_signal is Signal.WASTE_SUPPORTING
    )
    call_level_both_waste_supporting = (
        result_signal is Signal.WASTE_SUPPORTING and write_signal is Signal.WASTE_SUPPORTING
    )

    if terminal_signal is Signal.LEGIT_SUPPORTING and not call_level_any_waste_supporting:
        return Classification(
            pair=pair,
            verdict=Verdict.LIKELY_LEGITIMATE,
            result_signal=result_signal,
            write_signal=write_signal,
            terminal_signal=terminal_signal,
            reason=terminal_note,
        )

    all_waste_supporting = (
        call_level_both_waste_supporting and terminal_signal is Signal.WASTE_SUPPORTING
    )
    if all_waste_supporting:
        return Classification(
            pair=pair,
            verdict=Verdict.CONFIRMED_WASTE,
            result_signal=result_signal,
            write_signal=write_signal,
            terminal_signal=terminal_signal,
            reason=f"{result_note}; {write_note}; {terminal_note}",
        )

    if terminal_signal is Signal.LEGIT_SUPPORTING and call_level_any_waste_supporting:
        # The case terminal success can't rescue: at least one call-level
        # signal confirmed waste (the other may be confirmed too, or just
        # unknown), task succeeded anyway. Spelled out explicitly rather
        # than folded into the generic reason below -- this is the
        # specific tension the verdict is resolving, not an absence of
        # information.
        reason = (
            f"{result_note}; {write_note}; task succeeded, but that doesn't confirm "
            "this specific repeated call contributed -- task-level outcome isn't a "
            "substitute for the missing call-level answer"
        )
    else:
        unknowns = [
            note
            for signal, note in (
                (result_signal, result_note),
                (write_signal, write_note),
                (terminal_signal, terminal_note),
            )
            if signal is Signal.UNKNOWN
        ]
        reason = "no legitimate-use signal fired, but " + "; ".join(unknowns) if unknowns else (
            f"{result_note}; {write_note}; {terminal_note}"
        )

    return Classification(
        pair=pair,
        verdict=Verdict.UNCLASSIFIED,
        result_signal=result_signal,
        write_signal=write_signal,
        terminal_signal=terminal_signal,
        reason=reason,
    )


def _result_signal(pair: CandidatePair, lineage: TaskLineage) -> tuple[Signal, str]:
    original_result = _correlated_result_hash(pair.original, lineage)
    repeat_result = _correlated_result_hash(pair.repeat, lineage)

    if original_result is None or repeat_result is None:
        return Signal.UNKNOWN, "result not observable for one or both calls"
    if original_result == repeat_result:
        return Signal.WASTE_SUPPORTING, "result identical"
    return Signal.LEGIT_SUPPORTING, "result changed"


def _correlated_result_hash(call: Event, lineage: TaskLineage) -> str | None:
    if call.event_type == "tool_call":
        for child in lineage.children_of(call):
            if child.event_type == "tool_result":
                return child.content_hash
        return None
    if call.event_type == "llm_call":
        # No llm_result event type exists in the schema -- an LLM's
        # completion is only comparable if the source opted into recording
        # a response_hash via the metadata escape hatch.
        value = call.metadata.get(META_RESPONSE_HASH_KEY)
        return str(value) if value is not None else None
    return None


def _write_signal(pair: CandidatePair) -> tuple[Signal, str]:
    saw_unknown = False
    for event in pair.intervening:
        if event.event_type == "tool_result":
            # tool_result rows are outcomes, not actions -- they can't
            # independently constitute a side effect, so they're not asked
            # for a write flag.
            continue
        write = event.metadata.get(META_WRITE_KEY)
        if write is True:
            return Signal.LEGIT_SUPPORTING, f"write intervened at step {event.step_index}"
        if write is False:
            continue  # explicitly confirmed no side effect, keep scanning
        if event.event_type == "llm_call":
            # No write flag recorded, but llm_call is structurally a model
            # completion, not an action -- the schema has no mechanism for
            # a pure generation step to mutate external state on its own;
            # anything it causes to happen shows up as a separate tool_call
            # event. Same reasoning as _correlated_result_hash() treating
            # llm_call's missing response_hash as definitionally unknowable
            # rather than "maybe present, just not captured": here the
            # event type itself rules out a write, so absence isn't
            # ambiguous. Only tool_call absence stays genuinely unknown,
            # since a tool can plausibly do anything.
            continue
        saw_unknown = True

    if saw_unknown:
        return Signal.UNKNOWN, "write status not recorded for at least one intervening tool call"
    return Signal.WASTE_SUPPORTING, "no intervening write"


def _terminal_signal(lineage: TaskLineage) -> tuple[Signal, str]:
    outcome = lineage.terminal_outcome
    if outcome == "error":
        return Signal.WASTE_SUPPORTING, "task terminated in failure"
    if outcome == "ok":
        return Signal.LEGIT_SUPPORTING, "task terminated in success"
    return Signal.UNKNOWN, "task terminal outcome not recorded"
