import json
import sqlite3

import pytest

from redundo.adapter.base import InputLoadError
from redundo.adapter.cli import main
from redundo.adapter.pricing import PricingContext, PricingEntry
from redundo.adapter.sources.opencode import OpenCodeSource, convert_opencode, load_opencode

NO_PRICING = PricingContext(entries={}, generated_at=None)


def _tokens(input=100, output=20, reasoning=0, read=0, write=0):
    return {"input": input, "output": output, "reasoning": reasoning, "cache": {"read": read, "write": write}}


class Session:
    """Builds an OpenCode 1.x session the way `opencode export` prints one:
    {"info": Session, "messages": [{"info": Message, "parts": [Part]}]},
    with time-sortable ids like OpenCode's own."""

    def __init__(self, session_id="ses_aaa", parent_id=None):
        self.info = {"id": session_id, "slug": "s", "title": "t", "directory": "/proj",
                     "version": "1.19.5", "time": {"created": 1, "updated": 1}}
        if parent_id:
            self.info["parentID"] = parent_id
        self.messages = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}_{self.info['id'][-3:]}{self._n:05d}"

    def user(self, text, parts=()):
        msg_id = self._id("msg")
        self.messages.append({
            "info": {"id": msg_id, "sessionID": self.info["id"], "role": "user",
                     "time": {"created": self._n}, "agent": "build",
                     "model": {"providerID": "anthropic", "modelID": "claude-sonnet-5"}},
            "parts": [{"id": self._id("prt"), "type": "text", "text": text}, *parts],
        })
        return msg_id

    def assistant(self, *steps, agent="build", model="claude-sonnet-5", error=None, completed=True, **extra):
        """Each step: a list of ("text", str) / ("tool", name, input, status, output)
        items, optionally ending in ("finish", cost) and ("patch", [files])."""
        parent = next(m["info"]["id"] for m in reversed(self.messages) if m["info"]["role"] == "user")
        msg_id = self._id("msg")
        parts = []
        for step in steps:
            parts.append({"id": self._id("prt"), "type": "step-start", "snapshot": "abc"})
            for item in step:
                kind = item[0]
                pid = self._id("prt")
                if kind == "text":
                    parts.append({"id": pid, "type": "text", "text": item[1], "time": {"start": self._n}})
                elif kind == "tool":
                    _, name, args, status, output = item
                    state = {"status": status, "input": args, "time": {"start": self._n, "end": self._n + 1}}
                    if status == "completed":
                        state.update(output=output, title=name, metadata={"truncated": False})
                    elif status == "error":
                        state["error"] = output
                    parts.append({"id": pid, "type": "tool", "callID": f"call_{pid}", "tool": name, "state": state})
                elif kind == "finish":
                    parts.append({"id": pid, "type": "step-finish", "reason": "tool-calls",
                                  "cost": item[1], "tokens": item[2] if len(item) > 2 else _tokens()})
                elif kind == "patch":
                    parts.append({"id": pid, "type": "patch", "hash": "h", "files": item[1]})
                elif kind == "retry":
                    parts.append({"id": pid, "type": "retry", "attempt": 1,
                                  "error": {"name": "APIError", "data": {"message": "overloaded"}},
                                  "time": {"created": self._n}})
        info = {"id": msg_id, "sessionID": self.info["id"], "role": "assistant", "parentID": parent,
                "modelID": model, "providerID": "anthropic", "mode": agent, "agent": agent,
                "path": {"cwd": "/proj", "root": "/proj"}, "cost": 0, "tokens": _tokens(),
                "time": {"created": self._n, **({"completed": self._n + 1} if completed else {})},
                **extra}
        if error:
            info["error"] = error
        self.messages.append({"info": info, "parts": parts})
        return msg_id

    def export(self):
        return {"info": self.info, "messages": self.messages}

    def doc(self):
        return {"opencode_session": self.info, "messages": self.messages, "origin": "export"}


def _convert(*docs, pricing_context=NO_PRICING):
    return convert_opencode(list(docs), pricing_context=pricing_context)


def _types(records):
    return [(r["event_type"], r["name"]) for r in records]


def test_each_step_is_one_llm_call_with_its_own_cost_and_tools_follow_it():
    s = Session()
    s.user("fix the tests")
    s.assistant(
        [("text", "running tests"),
         ("tool", "bash", {"command": "pytest"}, "completed", "1 failed"),
         ("finish", 0.01, _tokens(input=10, output=5, reasoning=3, read=1000, write=50))],
        [("text", "done"), ("finish", 0.02)],
    )

    records, summary = _convert(s.doc())
    assert _types(records) == [
        ("llm_call", "claude-sonnet-5"), ("tool_call", "bash"), ("tool_result", "bash"),
        ("llm_call", "claude-sonnet-5"),
    ]
    assert [r["parent_id"] for r in records] == [None, 0, 1, 2]
    first, call, result, second = records
    assert first["cost_usd"] == pytest.approx(0.01)
    assert second["cost_usd"] == pytest.approx(0.02)
    assert first["tokens_in"] == 1060 and first["tokens_out"] == 8
    assert first["outcome"] == second["outcome"] == "ok"
    assert call["model"] == "claude-sonnet-5"
    assert call["metadata"]["model_basis"] == "issuing_llm_call"
    assert result["outcome"] == "ok"
    assert all(r["workflow"] == "build" for r in records)
    assert first["metadata"]["workflow_basis"] == "agent"
    assert summary.llm_calls == 2 and summary.llm_calls_priced_by_opencode == 2


def test_tool_error_and_interrupted_tools():
    s = Session()
    s.user("go")
    s.assistant([
        ("tool", "bash", {"command": "make"}, "error", "exit 2"),
        ("tool", "bash", {"command": "sleep 99"}, "running", None),
    ], completed=False)

    records, summary = _convert(s.doc())
    assert _types(records) == [
        ("llm_call", "claude-sonnet-5"), ("tool_call", "bash"), ("tool_result", "bash"), ("tool_call", "bash"),
    ]
    assert records[2]["outcome"] == "error"
    assert records[0]["outcome"] == "error"  # its step never finished
    assert summary.tool_calls_without_result == 1


def test_message_that_failed_before_any_step_uses_its_own_totals():
    s = Session()
    s.user("go")
    s.messages.append({
        "info": {"id": "msg_zzz", "sessionID": "ses_aaa", "role": "assistant",
                 "parentID": s.messages[0]["info"]["id"], "modelID": "m", "providerID": "p",
                 "mode": "build", "agent": "build", "path": {}, "cost": 0.003, "tokens": _tokens(),
                 "time": {"created": 99}, "error": {"name": "APIError", "data": {"message": "x"}}},
        "parts": [],
    })
    (llm,), _ = _convert(s.doc())
    assert llm["outcome"] == "error"
    assert llm["cost_usd"] == pytest.approx(0.003)
    assert llm["metadata"]["error_name"] == "APIError"


def test_identical_repeats_hash_the_same_and_steps_without_tools_differ_in_context():
    s = Session()
    s.user("check")
    s.assistant(
        [("tool", "bash", {"command": "pytest -q"}, "completed", "1 failed"), ("finish", 0.01)],
        [("tool", "bash", {"command": "pytest -q"}, "completed", "1 failed"), ("finish", 0.01)],
    )
    records, _ = _convert(s.doc())
    calls = [r for r in records if r["event_type"] == "tool_call"]
    results = [r for r in records if r["event_type"] == "tool_result"]
    llms = [r for r in records if r["event_type"] == "llm_call"]
    assert calls[0]["content_hash"] == calls[1]["content_hash"]
    assert results[0]["content_hash"] == results[1]["content_hash"]
    assert llms[0]["content_hash"] != llms[1]["content_hash"]


def test_similarity_fingerprint_covers_the_new_input_only():
    from redundo.adapter.hashing import similarity_fingerprint

    s = Session()
    s.user("first")
    s.assistant([("text", "a"), ("finish", 0.01)])
    s.user("second")
    s.assistant([("text", "b"), ("finish", 0.01)])
    records, _ = _convert(s.doc())
    expected = similarity_fingerprint(
        [{"role": "user", "parts": [{"type": "text", "text": "second"}]}], structured=True
    )
    assert records[1]["metadata"]["similarity_fingerprint"] == expected


def test_one_message_per_step_still_fingerprints_the_tool_results():
    # OpenCode 1.19 writes each step of a turn as its own assistant message.
    s = Session()
    s.user("fix it")
    s.assistant([("tool", "bash", {"command": "pytest"}, "completed", "1 failed"), ("finish", 0.01)])
    s.assistant([("tool", "read", {"filePath": "a.py"}, "completed", "x = 1"), ("finish", 0.01)])
    s.assistant([("text", "done"), ("finish", 0.01)])
    records, _ = _convert(s.doc())
    fingerprints = [r["metadata"]["similarity_fingerprint"] for r in records if r["event_type"] == "llm_call"]
    assert len(set(fingerprints)) == 3


def test_compaction_cuts_the_context_like_filter_compacted():
    def build(session_id, early):
        s = Session(session_id=session_id)
        s.user(early)
        s.assistant([("text", "early answer"), ("finish", 0.01)])
        compaction_user = s.user("", parts=[{"id": "prt_c", "type": "compaction", "auto": True}])
        s.messages[-1]["parts"] = [{"id": "prt_c", "type": "compaction", "auto": True}]
        s.assistant([("text", "summary of everything"), ("finish", 0.01)],
                    agent="compaction", summary=True, finish="stop")
        s.user("next")
        s.assistant([("text", "after"), ("finish", 0.01)])
        return s.doc(), compaction_user

    (one, _), (two, _) = build("ses_one", "alpha"), build("ses_two", "beta")
    a, _ = _convert(one)
    b, _ = _convert(two)
    assert a[0]["content_hash"] != b[0]["content_hash"]
    assert a[-1]["content_hash"] == b[-1]["content_hash"]
    assert a[1]["workflow"] == "compaction"


def test_subagent_session_links_back_as_a_delegation():
    parent = Session(session_id="ses_par")
    parent.user("research this")
    parent.assistant([("tool", "task", {"prompt": "look", "subagent_type": "explore"}, "completed", "found"),
                      ("finish", 0.01)])
    parent.messages[-1]["parts"][1]["state"]["metadata"] = {"sessionId": "ses_kid"}
    child = Session(session_id="ses_kid", parent_id="ses_par")
    child.user("look")
    child.assistant([("text", "found"), ("finish", 0.004)], agent="explore")

    records, summary = _convert(parent.doc(), child.doc())
    task_call = next(r for r in records if r["name"] == "task")
    assert task_call["metadata"]["child_task_id"] == "ses_kid"
    kid = [r for r in records if r["task_id"] == "ses_kid"]
    assert kid[0]["workflow"] == "explore"
    assert kid[0]["metadata"]["parent_task_id"] == "ses_par"
    assert kid[0]["metadata"]["parent_task_link_kind"] == "delegation"
    assert "parent_task_id" not in records[0]["metadata"]
    assert summary.subagent_sessions == 1


def test_patch_marks_a_write_only_when_one_tool_could_have_made_it():
    s = Session()
    s.user("edit")
    s.assistant(
        [("tool", "edit", {"filePath": "a.py"}, "completed", "ok"), ("finish", 0.01), ("patch", ["a.py"])],
        [("tool", "edit", {"filePath": "b.py"}, "completed", "ok"),
         ("tool", "bash", {"command": "ls"}, "completed", "a.py"), ("finish", 0.01), ("patch", ["b.py"])],
        [("tool", "read", {"filePath": "a.py"}, "completed", "x"), ("finish", 0.01)],
    )
    records, summary = _convert(s.doc())
    calls = [r for r in records if r["event_type"] == "tool_call"]
    assert calls[0]["metadata"]["write"] is True
    assert calls[0]["metadata"]["write_basis"] == "step_patch_single_tool"
    assert all("write" not in c["metadata"] for c in calls[1:])
    assert summary.tool_calls_marked_write == 1
    assert summary.patch_steps_with_several_tools == 1


def test_zero_cost_with_tokens_falls_back_to_the_table_or_stays_unpriced():
    s = Session()
    s.user("hi")
    s.assistant([("text", "x"), ("finish", 0)])
    (llm,), summary = _convert(s.doc())
    assert llm["cost_usd"] is None and summary.llm_calls_unpriced == 1

    table = PricingContext(
        entries={"claude-sonnet-5": PricingEntry(provider="anthropic", input=1e-6, output=2e-6)},
        generated_at="2026-10-01T00:00:00Z",
    )
    (llm,), _ = _convert(s.doc(), pricing_context=table)
    assert llm["cost_usd"] == pytest.approx(100e-6 + 20 * 2e-6)
    assert llm["metadata"]["cost_basis"] == "estimated_from_bundled_pricing_table"


def test_sanitized_exports_hash_as_opaque_never_as_changed_content():
    s = Session()
    s.user("[redacted:text:prt_1]")
    s.assistant(
        [("tool", "bash", {"redacted": "tool-input:prt_2"}, "completed", "[redacted:tool-output:prt_2]"),
         ("finish", 0.01)],
        [("tool", "bash", {"redacted": "tool-input:prt_3"}, "completed", "[redacted:tool-output:prt_3]"),
         ("finish", 0.01)],
    )
    records, summary = _convert(s.doc())
    assert all(r["metadata"]["content_basis"] == "opaque" for r in records)
    assert all("similarity_fingerprint" not in r["metadata"] for r in records)
    assert summary.records_with_redacted_content == len(records)


def test_retry_parts_are_counted_not_emitted():
    s = Session()
    s.user("hi")
    s.assistant([("retry",), ("text", "x"), ("finish", 0.01)])
    records, summary = _convert(s.doc())
    assert _types(records) == [("llm_call", "claude-sonnet-5")]
    assert summary.retry_parts == 1


def test_the_same_session_twice_is_one_task():
    s = Session()
    s.user("hi")
    s.assistant([("text", "x"), ("finish", 0.01)])
    records, summary = _convert(s.doc(), s.doc())
    assert len(records) == 1 and summary.sessions_total == 1


# --- loading -----------------------------------------------------------------


def _write_database(path, *sessions, v2_only=False):
    conn = sqlite3.connect(path)
    if v2_only:
        conn.executescript(
            "CREATE TABLE session_v2 (id text PRIMARY KEY);"
            "CREATE TABLE session_message (id text PRIMARY KEY, data text);"
        )
        conn.close()
        return path
    conn.executescript("""
        CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
            slug text NOT NULL, directory text NOT NULL, title text NOT NULL, version text NOT NULL,
            agent text, model text, time_created integer NOT NULL, time_updated integer NOT NULL);
        CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
        CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
    """)
    for s in sessions:
        conn.execute("INSERT INTO session VALUES (?, 'prj', ?, 's', '/proj', 't', '1.19.5', NULL, NULL, 1, 1)",
                     (s.info["id"], s.info.get("parentID")))
        for m in s.messages:
            data = {k: v for k, v in m["info"].items() if k not in ("id", "sessionID")}
            conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                         (m["info"]["id"], s.info["id"], m["info"]["time"]["created"], 1, json.dumps(data)))
            for p in m["parts"]:
                data = {k: v for k, v in p.items() if k not in ("id", "sessionID", "messageID")}
                conn.execute("INSERT INTO part VALUES (?, ?, ?, 1, 1, ?)",
                             (p["id"], m["info"]["id"], s.info["id"], json.dumps(data)))
    conn.commit()
    conn.close()
    return path


def _sample(session_id="ses_aaa"):
    s = Session(session_id=session_id)
    s.user("go")
    s.assistant([("tool", "bash", {"command": "ls"}, "completed", "x"), ("finish", 0.01)])
    return s


def test_database_and_export_of_the_same_session_convert_identically(tmp_path):
    s = _sample()
    db = _write_database(tmp_path / "opencode.db", s)
    export = tmp_path / "s.json"
    export.write_text(json.dumps(s.export()), encoding="utf-8")

    from_db, _ = _convert(*load_opencode(db))
    from_export, _ = _convert(*load_opencode(export))
    strip = lambda rs: [(r["event_type"], r["content_hash"], r["cost_usd"], r["parent_id"]) for r in rs]
    assert strip(from_db) == strip(from_export)
    assert len(from_db) == 3


def test_database_is_opened_read_only(tmp_path):
    db = _write_database(tmp_path / "opencode.db", _sample())
    db.chmod(0o444)
    try:
        assert load_opencode(db)
    finally:
        db.chmod(0o644)


def test_v2_only_database_is_refused_clearly(tmp_path):
    db = _write_database(tmp_path / "opencode.db", v2_only=True)
    with pytest.raises(InputLoadError, match="OpenCode 2.x database"):
        load_opencode(db)


def test_unrelated_sqlite_and_json_are_not_claimed(tmp_path):
    conn = sqlite3.connect(tmp_path / "other.db")
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    assert load_opencode(tmp_path / "other.db") is None
    (tmp_path / "x.json").write_text('{"info": {"id": "nope"}, "messages": []}', encoding="utf-8")
    assert load_opencode(tmp_path / "x.json") is None
    assert load_opencode(tmp_path) is None


def test_detect_only_claims_opencode_documents():
    assert OpenCodeSource().detect([_sample().doc()]).source == "opencode"
    assert OpenCodeSource().detect([{"pi_session_file": "x"}]) is None


def test_cli_reads_a_database_and_a_directory_of_exports(tmp_path, capsys):
    db = _write_database(tmp_path / "opencode.db", _sample())
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "a.json").write_text(json.dumps(_sample("ses_one").export()), encoding="utf-8")
    (exports / "b.json").write_text(json.dumps(_sample("ses_two").export()), encoding="utf-8")

    assert main([str(db), "--summary"]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 3
    assert "source: opencode (detected via opencode source's own native loader)" in captured.err

    assert main([str(exports)]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {r["task_id"] for r in records} == {"ses_one", "ses_two"}


def test_cli_reports_a_v2_database_without_crashing(tmp_path, capsys):
    db = _write_database(tmp_path / "opencode.db", v2_only=True)
    assert main([str(db)]) == 1
    assert "OpenCode 2.x database" in capsys.readouterr().err
