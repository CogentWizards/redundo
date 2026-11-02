import json

import pytest

from redundo.adapter.base import InputLoadError
from redundo.adapter.cli import main
from redundo.adapter.pricing import PricingContext, PricingEntry
from redundo.adapter.sources.pi import PiSource, convert_pi, load_pi_sessions

NO_PRICING = PricingContext(entries={}, generated_at=None)


def _usage(cost=0.01, input=100, output=20, cache_read=0, cache_write=0):
    return {
        "input": input, "output": output, "cacheRead": cache_read, "cacheWrite": cache_write,
        "totalTokens": input + output + cache_read + cache_write,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost},
    }


class Session:
    """Builds a Pi v3 session file the way Pi writes one: a header line,
    then append-only entries chained by parentId."""

    def __init__(self, session_id="sess-1", hour=10, parent_session=None):
        self.hour = hour
        self.header = {"type": "session", "version": 3, "id": session_id,
                       "timestamp": f"2026-11-01T{hour:02d}:00:00.000Z", "cwd": "/proj"}
        if parent_session:
            self.header["parentSession"] = parent_session
        self.entries = []
        self.leaf = None
        self._n = 0
        self._clock = 0

    def _ts(self):
        self._clock += 1
        iso = f"2026-11-01T{self.hour:02d}:00:{self._clock:02d}.000Z"
        return iso, 1793527200000 + (self.hour - 10) * 3600_000 + self._clock * 1000

    def add(self, entry, parent="leaf"):
        self._n += 1
        iso, _ = self._ts()
        entry = {"id": f"{self.header['id'][:4]}{self._n:04d}",
                 "parentId": self.leaf if parent == "leaf" else parent,
                 "timestamp": iso, **entry}
        self.entries.append(entry)
        self.leaf = entry["id"]
        return entry["id"]

    def message(self, message, parent="leaf"):
        _, ms = self._ts()
        return self.add({"type": "message", "message": {"timestamp": ms, **message}}, parent)

    def user(self, text, parent="leaf"):
        return self.message({"role": "user", "content": text}, parent)

    def assistant(self, text=None, tool_calls=(), stop="stop", cost=0.01, model="claude-sonnet-5",
                  parent="leaf", **extra):
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for call_id, name, args in tool_calls:
            content.append({"type": "toolCall", "id": call_id, "name": name, "arguments": args})
        return self.message({
            "role": "assistant", "content": content, "api": "anthropic-messages",
            "provider": "anthropic", "model": model, "usage": _usage(cost=cost),
            "stopReason": "toolUse" if tool_calls and stop == "stop" else stop, **extra,
        }, parent)

    def tool_result(self, call_id, name, output, is_error=False, parent="leaf", **extra):
        return self.message({
            "role": "toolResult", "toolCallId": call_id, "toolName": name,
            "content": [{"type": "text", "text": output}], "isError": is_error, **extra,
        }, parent)

    def write(self, path):
        lines = [json.dumps(self.header)] + [json.dumps(e) for e in self.entries]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def doc(self, path="/sessions/a.jsonl", parent_session_id=None):
        doc = {"pi_session_file": path, "header": self.header, "entries": self.entries}
        if parent_session_id:
            doc["parent_session_id"] = parent_session_id
        return doc


def _convert(*docs, pricing_context=NO_PRICING):
    return convert_pi(list(docs), pricing_context=pricing_context)


def _by_type(records, event_type):
    return [r for r in records if r["event_type"] == event_type]


def test_parallel_tool_calls_chain_linearly_and_join_by_tool_call_id():
    s = Session()
    s.user("list files and read the readme")
    s.assistant(tool_calls=[("c1", "bash", {"command": "ls"}), ("c2", "read", {"path": "README.md"})])
    # Results land in the opposite order to the calls.
    s.tool_result("c2", "read", "# hello")
    s.tool_result("c1", "bash", "README.md")
    s.assistant("done")

    records, summary = _convert(s.doc())
    shape = [(r["step_index"], r["event_type"], r["name"], r["parent_id"]) for r in records]
    assert shape == [
        (0, "llm_call", "claude-sonnet-5", None),
        (1, "tool_call", "read", 0),
        (2, "tool_result", "read", 1),
        (3, "tool_call", "bash", 2),
        (4, "tool_result", "bash", 3),
        (5, "llm_call", "claude-sonnet-5", 4),
    ]
    assert records[1]["metadata"]["tool_call_id"] == "c2"
    assert records[2]["metadata"]["tool_call_id"] == "c2"
    assert summary.llm_calls == 2 and summary.tool_calls == 2


def test_llm_call_carries_pi_cost_tokens_and_model():
    s = Session()
    s.user("hi")
    s.message({"role": "assistant", "content": [{"type": "text", "text": "hello"}],
               "provider": "anthropic", "model": "claude-sonnet-5",
               "usage": _usage(cost=0.0123, input=10, output=5, cache_read=1000, cache_write=50),
               "stopReason": "stop"})

    (llm,), summary = _convert(s.doc())
    assert llm["cost_usd"] == pytest.approx(0.0123)
    assert "cost_basis" not in llm["metadata"]
    assert llm["tokens_in"] == 1060
    assert llm["tokens_out"] == 5
    assert llm["model"] == "claude-sonnet-5"
    assert llm["outcome"] == "ok"
    assert llm["workflow"] == "main"
    assert llm["metadata"]["workflow_basis"] == "no_delegation"
    assert llm["metadata"]["task_id_source"] == "session_id"
    assert llm["metadata"]["content_basis"] == "prompt"
    assert llm["metadata"]["response_hash"]
    assert llm["task_id"] == "sess-1"
    assert summary.llm_calls_priced_by_pi == 1


def test_tool_records_take_the_issuing_calls_model():
    s = Session()
    s.user("go")
    s.assistant(tool_calls=[("c1", "bash", {"command": "ls"})], model="gpt-5.6")
    s.tool_result("c1", "bash", "out")

    records, _ = _convert(s.doc())
    for record in records[1:]:
        assert record["model"] == "gpt-5.6"
        assert record["metadata"]["model_basis"] == "issuing_llm_call"


def test_identical_tool_calls_hash_the_same_and_outcomes_come_from_is_error():
    s = Session()
    s.user("check")
    s.assistant(tool_calls=[("c1", "bash", {"command": "make test"})])
    s.tool_result("c1", "bash", "FAILED", is_error=True)
    s.assistant(tool_calls=[("c2", "bash", {"command": "make test"})])
    s.tool_result("c2", "bash", "FAILED", is_error=True)

    records, _ = _convert(s.doc())
    calls = _by_type(records, "tool_call")
    results = _by_type(records, "tool_result")
    assert calls[0]["content_hash"] == calls[1]["content_hash"]
    assert results[0]["content_hash"] == results[1]["content_hash"]
    assert [r["outcome"] for r in results] == ["error", "error"]


def test_auto_retry_hashes_the_same_context_as_the_attempt_it_repeats():
    s = Session()
    s.user("summarize")
    failed = s.assistant(stop="error", cost=0, errorMessage="overloaded")
    s.add({"type": "context_edit", "targetId": failed, "replacement": None})
    s.assistant("summary")

    records, _ = _convert(s.doc())
    first, retry = records
    assert first["outcome"] == "error"
    assert retry["outcome"] == "ok"
    assert retry["parent_id"] == first["step_index"]
    assert first["content_hash"] == retry["content_hash"]
    assert first["metadata"]["response_hash"] != retry["metadata"]["response_hash"]


def test_context_differs_once_the_conversation_moves_on():
    s = Session()
    s.user("one")
    s.assistant("a")
    s.user("two")
    s.assistant("b")

    first, second = _convert(s.doc())[0]
    assert first["content_hash"] != second["content_hash"]


def test_tree_branches_are_siblings_not_a_chain():
    s = Session()
    s.user("q")
    branch_point = s.assistant("first answer")
    s.user("follow-up A")
    s.assistant("A")
    s.user("follow-up B", parent=branch_point)
    s.assistant("B")

    records, _ = _convert(s.doc())
    root, a, b = records
    assert a["parent_id"] == root["step_index"]
    assert b["parent_id"] == root["step_index"]


def test_compaction_replaces_summarized_history_in_the_context_hash():
    # Two sessions with different pre-compaction history but the same
    # compaction summary and kept tail must hash the next call identically.
    def build(session_id, early_text):
        s = Session(session_id=session_id)
        s.user(early_text)
        s.assistant("early answer")
        kept = s.user("keep this")
        s.assistant("kept answer")
        s.add({"type": "compaction", "summary": "earlier: stuff", "firstKeptEntryId": kept,
               "tokensBefore": 5000})
        s.user("next")
        s.assistant("after")
        return s.doc()

    one, _ = _convert(build("s-one", "alpha"))
    two, _ = _convert(build("s-two", "beta"))
    assert one[0]["content_hash"] != two[0]["content_hash"]
    assert one[-1]["content_hash"] == two[-1]["content_hash"]


def test_tool_call_without_a_result_is_a_leaf_after_its_llm_call():
    s = Session()
    s.user("go")
    s.assistant(tool_calls=[("c1", "bash", {"command": "sleep 100"})], stop="aborted")
    s.user("never mind")
    s.assistant("ok")

    records, summary = _convert(s.doc())
    shape = [(r["event_type"], r["parent_id"]) for r in records]
    assert shape == [("llm_call", None), ("tool_call", 0), ("llm_call", 0)]
    assert records[0]["outcome"] == "error"
    assert summary.tool_calls_without_result == 1


def test_zero_cost_with_tokens_is_unpriced_or_estimated_never_a_real_zero():
    s = Session()
    s.user("hi")
    s.assistant("hello", cost=0, model="claude-sonnet-5")

    (llm,), summary = _convert(s.doc())
    assert llm["cost_usd"] is None
    assert summary.llm_calls_unpriced == 1

    table = PricingContext(
        entries={"claude-sonnet-5": PricingEntry(provider="anthropic", input=1e-6, output=2e-6)},
        generated_at="2026-10-01T00:00:00Z",
    )
    (llm,), summary = _convert(s.doc(), pricing_context=table)
    assert llm["cost_usd"] == pytest.approx(100 * 1e-6 + 20 * 2e-6)
    assert llm["metadata"]["cost_basis"] == "estimated_from_bundled_pricing_table"
    assert summary.llm_calls_priced_by_bundled_table == 1


def test_usage_with_no_call_becomes_a_synthesized_cost_only_record():
    s = Session()
    s.user("hi")
    s.assistant("hello")
    s.add({"type": "usage", "kind": "cache_warm", "provider": "anthropic",
           "model": "claude-sonnet-5", "usage": _usage(cost=0.015, input=0, output=0, cache_read=50000)})
    s.add({"type": "compaction", "summary": "s", "firstKeptEntryId": "x", "tokensBefore": 1,
           "usage": _usage(cost=0.002)})
    s.tool_result("orphan", "bash", "x", usage=_usage(cost=0.003))

    records, summary = _convert(s.doc())
    synthesized = [r for r in records if r["metadata"].get("synthesized_cost_only")]
    assert [r["metadata"]["usage_kind"] for r in synthesized] == ["cache_warm", "compaction", "tool_nested"]
    assert synthesized[0]["cost_usd"] == pytest.approx(0.015)
    assert synthesized[0]["model"] == "claude-sonnet-5"
    assert len({r["content_hash"] for r in synthesized}) == 3
    assert summary.cost_usd_synthesized_from_usage_only == pytest.approx(0.02)


def test_fork_skips_copied_entries_and_links_back_as_a_continuation(tmp_path):
    parent = Session(session_id="parent-1")
    parent.user("q")
    parent.assistant(tool_calls=[("c1", "bash", {"command": "ls"})])
    parent.tool_result("c1", "bash", "a b")
    parent.assistant("done")
    parent_path = parent.write(tmp_path / "2026-11-01_parent-1.jsonl")

    # Pi's forkFrom(): new header stamped at fork time, every source entry
    # copied verbatim (same ids, same timestamps), then new work appended.
    child = Session(session_id="child-1", hour=11, parent_session=str(parent_path))
    child.entries = list(parent.entries)
    child.leaf = parent.leaf
    child._n = 100
    child.user("more")
    child.assistant(tool_calls=[("c9", "bash", {"command": "ls"})])
    child.tool_result("c9", "bash", "a b")
    child.write(tmp_path / "2026-11-01_child-1.jsonl")

    documents = load_pi_sessions(tmp_path)
    records, summary = _convert(*documents)

    child_records = [r for r in records if r["task_id"] == "child-1"]
    assert [r["event_type"] for r in child_records] == ["llm_call", "tool_call", "tool_result"]
    assert child_records[0]["parent_id"] is None
    for r in child_records:
        assert r["metadata"]["parent_task_id"] == "parent-1"
        assert r["metadata"]["parent_task_link_kind"] == "continuation"
    assert summary.forked_sessions == 1
    assert summary.copied_entries_skipped == len(parent.entries)
    # The copied history still shaped the child's context: its first call
    # can't hash like a fresh session's.
    fresh = Session(session_id="fresh")
    fresh.user("more")
    fresh.assistant("x")
    assert _convert(fresh.doc())[0][0]["content_hash"] != child_records[0]["content_hash"]


def test_fork_parent_id_resolves_from_the_header_when_parent_isnt_loaded(tmp_path):
    parent = Session(session_id="parent-2")
    parent.user("q")
    parent_path = parent.write(tmp_path / "parent.jsonl")
    child_dir = tmp_path / "other-project"
    child_dir.mkdir()
    child = Session(session_id="child-2", hour=11, parent_session=str(parent_path))
    child.write(child_dir / "child.jsonl")

    (doc,) = load_pi_sessions(child_dir)
    assert doc["parent_session_id"] == "parent-2"

    parent_path.unlink()
    (doc,) = load_pi_sessions(child_dir)
    assert doc["parent_session_id"] is None


def test_namespaced_tool_names_and_volatile_ids_dont_split_hashes():
    s = Session()
    s.user("go")
    s.message({"role": "assistant", "content": [
        {"type": "toolCall", "id": "call_A", "name": "search", "namespace": "web",
         "arguments": {"q": "x"}, "thoughtSignature": "sig-1"}],
        "model": "m", "usage": _usage(), "stopReason": "toolUse"})
    s.tool_result("call_A", "search", "r")

    other = Session(session_id="sess-2")
    other.user("go")
    other.message({"role": "assistant", "content": [
        {"type": "toolCall", "id": "call_B", "name": "search", "namespace": "web",
         "arguments": {"q": "x"}, "thoughtSignature": "sig-2"}],
        "model": "m", "usage": _usage(cost=0.5), "stopReason": "toolUse"})
    other.tool_result("call_B", "search", "r")
    other.user("again")
    other.assistant("y")
    s.user("again")
    s.assistant("y")

    a, _ = _convert(s.doc())
    b, _ = _convert(other.doc())
    assert a[1]["name"] == "web.search" and a[2]["name"] == "web.search"
    assert a[-1]["content_hash"] == b[-1]["content_hash"]


def test_v1_linear_sessions_without_ids_chain_in_file_order():
    doc = {
        "pi_session_file": "/x.jsonl",
        "header": {"type": "session", "id": "old", "timestamp": "2025-01-01T00:00:00Z"},
        "entries": [
            {"type": "message", "timestamp": "2025-01-01T00:00:01Z",
             "message": {"role": "user", "content": "q"}},
            {"type": "message", "timestamp": "2025-01-01T00:00:02Z",
             "message": {"role": "assistant", "content": [], "model": "m",
                         "usage": _usage(), "stopReason": "stop"}},
            {"type": "message", "timestamp": "2025-01-01T00:00:03Z",
             "message": {"role": "assistant", "content": [], "model": "m",
                         "usage": _usage(), "stopReason": "stop"}},
        ],
    }
    records, _ = _convert(doc)
    assert [r["parent_id"] for r in records] == [None, 0]


def test_loader_ignores_non_pi_files_and_returns_none_when_nothing_matches(tmp_path):
    (tmp_path / "notes.jsonl").write_text('{"hello": 1}\n', encoding="utf-8")
    assert load_pi_sessions(tmp_path) is None
    assert load_pi_sessions(tmp_path / "notes.jsonl") is None

    Session().write(tmp_path / "s.jsonl")
    documents = load_pi_sessions(tmp_path)
    assert [d["header"]["id"] for d in documents] == ["sess-1"]


def test_loader_tolerates_a_partial_last_line_but_not_a_corrupt_middle(tmp_path):
    s = Session()
    s.user("q")
    s.assistant("a")
    path = s.write(tmp_path / "s.jsonl")
    path.write_text(path.read_text(encoding="utf-8") + '{"type":"mess', encoding="utf-8")
    (doc,) = load_pi_sessions(path)
    assert len(doc["entries"]) == 2

    lines = path.read_text(encoding="utf-8").split("\n")
    lines.insert(2, "not json")
    path.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(InputLoadError) as excinfo:
        load_pi_sessions(path)
    assert ":3:" in str(excinfo.value)
    assert "not json" not in str(excinfo.value)


def test_detect_only_claims_pi_documents():
    s = Session()
    assert PiSource().detect([s.doc()]).source == "pi"
    assert PiSource().detect([{"resourceSpans": []}]) is None


def test_cli_reads_a_session_file_and_a_session_directory(tmp_path, capsys):
    s = Session()
    s.user("q")
    s.assistant(tool_calls=[("c1", "bash", {"command": "ls"})])
    s.tool_result("c1", "bash", "x")
    path = s.write(tmp_path / "s.jsonl")

    for target in (path, tmp_path):
        assert main([str(target), "--summary"]) == 0
        captured = capsys.readouterr()
        records = [json.loads(line) for line in captured.out.splitlines()]
        assert [r["event_type"] for r in records] == ["llm_call", "tool_call", "tool_result"]
        assert all(r["metadata"]["source_path"] == str(target.resolve()) for r in records)
        assert "source: pi (detected via pi source's own native loader)" in captured.err


def test_cli_reports_a_malformed_session_without_its_content(tmp_path, capsys):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(Session().header) + "\nsecret-content\n{}\n", encoding="utf-8")
    assert main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "s.jsonl:2: not valid JSON" in err
    assert "secret-content" not in err


def test_otlp_directory_still_wins_over_native_loaders(tmp_path, capsys):
    from helpers import span, traces_document

    Session().write(tmp_path / "s.jsonl")
    doc = traces_document([
        span("s1", start=0, attributes={"openinference.span.kind": "LLM", "input.value": "hi"}),
    ])
    (tmp_path / "traces-1.json").write_text(json.dumps(doc), encoding="utf-8")
    assert main([str(tmp_path), "--summary"]) == 0
    assert "source: openinference" in capsys.readouterr().err


def test_similarity_fingerprint_covers_only_the_new_input():
    from redundo.adapter.hashing import similarity_fingerprint

    s = Session()
    s.user("first question about the build")
    s.assistant("a")
    s.user("second, unrelated question")
    s.assistant("b")

    first, second = _convert(s.doc())[0]
    expected = similarity_fingerprint(
        [{"role": "user", "content": "second, unrelated question"}], structured=True
    )
    assert second["metadata"]["similarity_fingerprint"] == expected
    assert first["metadata"]["similarity_fingerprint"] != expected
