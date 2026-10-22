"""Resilience tests for the read-only Hermes state collector.

The collector is the only code that touches Hermes' SQLite files and logs. Its
contract is that a damaged, absent, locked or hostile input degrades the panel
instead of raising -- a dashboard that crashes on one bad file is worse than
one that shows less. ``test_state.py`` covers the happy path only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_eink_dashboard.state import (
    DashboardConfig,
    DashboardSnapshot,
    HermesStateCollector,
)

SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, ended_at REAL,
    message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
    output_tokens INTEGER, cache_read_tokens INTEGER, title TEXT,
    display_name TEXT, archived INTEGER DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
    tool_name TEXT, timestamp REAL
);
"""

FACTS_DDL = """
CREATE TABLE facts (id INTEGER PRIMARY KEY, content TEXT, trust_score REAL,
                    retrieval_count INTEGER);
"""


def _config(tmp_path: Path, **overrides) -> DashboardConfig:
    base = {
        "hermes_home": tmp_path,
        "state_db": tmp_path / "state.db",
        "kanban_db": tmp_path / "kanban.db",
        "memory_db": tmp_path / "memory_store.db",
        "memory_file": tmp_path / "memories" / "MEMORY.md",
        "user_file": tmp_path / "memories" / "USER.md",
        "agent_log": tmp_path / "logs" / "agent.log",
    }
    base.update(overrides)
    return DashboardConfig(**base)


def _collector(tmp_path: Path, now: float = 1000.0, **overrides) -> HermesStateCollector:
    return HermesStateCollector(_config(tmp_path, **overrides), now=lambda: now)


def _session_db(tmp_path: Path, *, ended_at: str = "NULL", input_tokens: int = 1024) -> None:
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.executescript(SESSIONS_DDL)
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("live", "tui", "gpt-test", 900, None if ended_at == "NULL" else float(ended_at),
         4, 1, input_tokens, 128, 0, "Work", None, 0),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        (1, "live", "user", "hello", None, 1000),
    )
    conn.commit()
    conn.close()


def _corrupt(path: Path) -> Path:
    """A file that exists and is non-empty but is not a database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is definitely not a sqlite database" * 8)
    return path


def _kanban_db(tmp_path: Path, rows: str = "") -> None:
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.executescript(
        "CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, priority INTEGER, updated_at REAL);\n"
        + rows
    )
    conn.commit()
    conn.close()


def _log(tmp_path: Path, text: str) -> None:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "agent.log").write_text(text, encoding="utf-8")


class TestFromHome:
    def test_derives_every_path_from_the_home(self, tmp_path: Path) -> None:
        config = DashboardConfig.from_home(tmp_path / "hermes")
        assert config.state_db.name == "state.db"
        assert config.kanban_db.name == "kanban.db"
        assert config.memory_db.name == "memory_store.db"
        assert config.memory_file == config.hermes_home / "memories" / "MEMORY.md"
        assert config.user_file == config.hermes_home / "memories" / "USER.md"
        assert config.agent_log == config.hermes_home / "logs" / "agent.log"

    def test_expands_the_home(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        config = DashboardConfig.from_home(Path("~/somewhere"))
        assert config.hermes_home.is_absolute()
        assert "~" not in str(config.hermes_home)

    def test_context_limit_override_is_applied(self, tmp_path: Path) -> None:
        config = DashboardConfig.from_home(tmp_path, context_limit=4096)
        assert config.context_limit == 4096

    def test_defaults_match_the_documented_caps(self, tmp_path: Path) -> None:
        config = DashboardConfig.from_home(tmp_path)
        assert (config.max_tasks, config.max_events) == (6, 5)


class TestEmptyHermesHome:
    """A fresh install has no databases at all; the panel must still render."""

    def test_missing_files_yield_an_idle_snapshot(self, tmp_path: Path) -> None:
        snapshot = _collector(tmp_path).collect()

        assert snapshot.session.id == ""
        assert snapshot.session.source == "idle"
        assert snapshot.session.model == "unknown"
        assert snapshot.session.title == "No active session"
        assert snapshot.session.status == "idle"
        assert snapshot.session.current_tool == "none"
        assert snapshot.session.context_percent == 0
        assert snapshot.session.context_limit == 262_144
        assert snapshot.tasks == ()
        assert snapshot.kanban_active == 0
        assert snapshot.memory.fact_count == 0
        assert snapshot.recent_events == ()

    def test_snapshot_serialises(self, tmp_path: Path) -> None:
        payload = _collector(tmp_path).collect().to_dict()
        assert {"session", "tasks", "generated_at"} <= set(payload)


class TestCorruptDatabases:
    def test_corrupt_state_db_is_survivable(self, tmp_path: Path) -> None:
        _corrupt(tmp_path / "state.db")
        snapshot = _collector(tmp_path).collect()
        assert snapshot.session.id == ""
        assert snapshot.session.model == "unknown"

    def test_corrupt_kanban_db_is_survivable(self, tmp_path: Path) -> None:
        _corrupt(tmp_path / "kanban.db")
        assert _collector(tmp_path).collect().kanban_active == 0

    def test_corrupt_memory_db_is_survivable(self, tmp_path: Path) -> None:
        _corrupt(tmp_path / "memory_store.db")
        assert _collector(tmp_path).collect().memory.fact_count == 0

    def test_all_databases_corrupt_still_collects(self, tmp_path: Path) -> None:
        for name in ("state.db", "kanban.db", "memory_store.db"):
            _corrupt(tmp_path / name)
        assert _collector(tmp_path).collect().session.status == "idle"

    def test_valid_state_db_survives_corrupt_siblings(self, tmp_path: Path) -> None:
        _session_db(tmp_path)
        _corrupt(tmp_path / "kanban.db")
        _corrupt(tmp_path / "memory_store.db")
        assert _collector(tmp_path).collect().session.model == "gpt-test"


class TestSchemaDrift:
    """Hermes is a moving target; a changed schema must not be fatal."""

    def test_unrelated_tables_only(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute("CREATE TABLE something_else (id INTEGER)")
        conn.commit()
        conn.close()
        assert _collector(tmp_path).collect().session.id == ""

    def test_dropped_columns_are_survivable(self, tmp_path: Path) -> None:
        """A future Hermes build dropping a column must not break the panel."""
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);")
        conn.execute("INSERT INTO sessions VALUES ('live', 'gpt-test')")
        conn.commit()
        conn.close()
        assert _collector(tmp_path).collect().session.id == ""

    def test_empty_tables_are_survivable(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.executescript(SESSIONS_DDL)
        conn.commit()
        conn.close()
        snapshot = _collector(tmp_path).collect()
        assert snapshot.session.id == ""
        assert snapshot.session.context_limit == 262_144

    def test_missing_facts_table_is_survivable(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "memory_store.db")
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        assert _collector(tmp_path).collect().memory.fact_count == 0


class TestSessionSelection:
    def test_archived_sessions_are_ignored(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.executescript(SESSIONS_DDL)
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("done", "tui", "old-model", 100, 200.0, 1, 0, 10, 1, 0, "Done", None, 1),
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("live", "tui", "gpt-test", 900, None, 4, 1, 1024, 128, 0, "W", None, 0),
        )
        conn.commit()
        conn.close()
        assert _collector(tmp_path).collect().session.id == "live"

    def test_the_most_recently_active_session_wins(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.executescript(SESSIONS_DDL)
        for session_id, activity in (("older", 500), ("newest", 999)):
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, "tui", "gpt-test", 100, None, 1, 0, 10, 1, 0, "W", None, 0),
            )
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (hash(session_id) % 1000, session_id, "user", "x", None, activity),
            )
        conn.commit()
        conn.close()
        assert _collector(tmp_path).collect().session.id == "newest"

    def test_the_latest_tool_is_reported(self, tmp_path: Path) -> None:
        _session_db(tmp_path)
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute(
            "INSERT INTO messages VALUES (2,'live','tool','{}','terminal',1010)"
        )
        conn.commit()
        conn.close()
        assert _collector(tmp_path).collect().session.current_tool == "terminal"


class TestSessionStatus:
    def test_recent_unended_session_is_working(self, tmp_path: Path) -> None:
        _session_db(tmp_path, ended_at="NULL", input_tokens=1024)
        assert _collector(tmp_path, now=1000.0).collect().session.status == "working"

    def test_stale_unended_session_is_idle(self, tmp_path: Path) -> None:
        """No activity for hours but never ended: report idle, not working."""
        _session_db(tmp_path, ended_at="NULL")
        assert _collector(tmp_path, now=9999.0).collect().session.status == "idle"

    def test_ended_session_is_idle(self, tmp_path: Path) -> None:
        _session_db(tmp_path, ended_at="950")
        assert _collector(tmp_path, now=1000.0).collect().session.status == "idle"

    def test_activity_older_than_started_at_does_not_go_negative(self, tmp_path: Path) -> None:
        _session_db(tmp_path, ended_at="NULL")
        assert _collector(tmp_path, now=500.0).collect().session.status == "working"


class TestContextLimitGuards:
    def test_zero_context_limit_cannot_divide_by_zero(self, tmp_path: Path) -> None:
        _session_db(tmp_path, input_tokens=1000)
        snapshot = _collector(tmp_path, context_limit=0).collect()
        assert snapshot.session.context_limit >= 1
        assert 0 <= snapshot.session.context_percent <= 100

    def test_negative_context_limit_is_clamped(self, tmp_path: Path) -> None:
        _session_db(tmp_path, input_tokens=1000)
        snapshot = _collector(tmp_path, context_limit=-5).collect()
        assert snapshot.session.context_limit == 1
        assert snapshot.session.context_percent == 100

    def test_percent_is_capped_at_100(self, tmp_path: Path) -> None:
        """A session reporting more tokens than the limit must not exceed 100%."""
        _session_db(tmp_path, input_tokens=10_000_000)
        snapshot = _collector(tmp_path, context_limit=1024).collect()
        assert snapshot.session.context_percent == 100

    def test_custom_limit_is_reported(self, tmp_path: Path) -> None:
        _session_db(tmp_path, input_tokens=256)
        snapshot = _collector(tmp_path, context_limit=1024).collect()
        assert snapshot.session.context_limit == 1024
        assert snapshot.session.context_percent == 25

    def test_api_log_overrides_cumulative_session_tokens(self, tmp_path: Path) -> None:
        """sessions.input_tokens accumulates; the last API record is the truth."""
        _session_db(tmp_path, input_tokens=999_999)
        _log(
            tmp_path,
            "2026-07-24 12:00:00 INFO hermes.llm: API call #7: model=gpt-test "
            "provider=openai in=1024 out=128 latency=1.2s\n",
        )
        snapshot = _collector(tmp_path, context_limit=1024).collect()
        assert snapshot.session.input_tokens == 1024
        assert snapshot.session.output_tokens == 128
        assert snapshot.session.context_percent == 100

    def test_api_log_for_a_different_model_is_ignored(self, tmp_path: Path) -> None:
        _session_db(tmp_path, input_tokens=512)
        _log(
            tmp_path,
            "2026-07-24 12:00:00 INFO hermes.llm: API call #7: model=other-model "
            "provider=openai in=9999 out=1 latency=1.2s\n",
        )
        assert _collector(tmp_path).collect().session.input_tokens == 512


class TestCleanText:
    def test_collapses_whitespace(self) -> None:
        assert HermesStateCollector._clean_text("a\n\n  b\t\tc") == "a b c"

    def test_truncates_with_an_ellipsis(self) -> None:
        cleaned = HermesStateCollector._clean_text("x" * 500, limit=20)
        assert len(cleaned) == 20
        assert cleaned.endswith("…")

    def test_exactly_at_the_limit_is_not_truncated(self) -> None:
        assert HermesStateCollector._clean_text("y" * 100, limit=100) == "y" * 100

    @pytest.mark.parametrize(
        "raw,secret",
        [
            ("token=abc123def", "abc123def"),
            ("password: hunter2", "hunter2"),
            ("api_key = zzz", "zzz"),
            ("API-KEY=zzz", "zzz"),
            ("secret: shhh", "shhh"),
        ],
    )
    def test_redacts_secret_shaped_values(self, raw: str, secret: str) -> None:
        cleaned = HermesStateCollector._clean_text(raw)
        assert "<redacted>" in cleaned
        assert secret not in cleaned

    def test_none_becomes_empty(self) -> None:
        assert HermesStateCollector._clean_text("") == ""

    def test_preserves_ordinary_text(self) -> None:
        assert HermesStateCollector._clean_text("build the dashboard") == "build the dashboard"


class TestTodoTasks:
    def _todo_session(self, tmp_path: Path, payload: str) -> None:
        _session_db(tmp_path)
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute(
            "INSERT INTO messages VALUES (2,'live','tool',?,'todo',1005)", (payload,)
        )
        conn.commit()
        conn.close()

    def test_open_todos_become_tasks(self, tmp_path: Path) -> None:
        self._todo_session(
            tmp_path,
            '{"todos": [{"content": "ship v1", "status": "pending"},'
            ' {"content": "done already", "status": "completed"}]}',
        )
        snapshot = _collector(tmp_path).collect()
        assert [task.title for task in snapshot.tasks] == ["ship v1"]
        assert snapshot.tasks[0].source == "session"

    def test_title_is_taken_from_the_first_task(self, tmp_path: Path) -> None:
        self._todo_session(
            tmp_path, '{"todos": [{"content": "ship v1", "status": "pending"}]}'
        )
        assert _collector(tmp_path).collect().session.title == "ship v1"

    def test_malformed_json_is_skipped(self, tmp_path: Path) -> None:
        self._todo_session(tmp_path, "{not json")
        assert _collector(tmp_path).collect().tasks == ()

    def test_non_dict_payload_is_skipped(self, tmp_path: Path) -> None:
        self._todo_session(tmp_path, "[1, 2, 3]")
        assert _collector(tmp_path).collect().tasks == ()

    def test_wrong_todos_type_is_skipped(self, tmp_path: Path) -> None:
        self._todo_session(tmp_path, '{"todos": "not a list"}')
        assert _collector(tmp_path).collect().tasks == ()

    def test_only_the_newest_todo_message_is_used(self, tmp_path: Path) -> None:
        """A stale todo list from earlier in the session must not resurface."""
        self._todo_session(tmp_path, '{"todos": [{"content": "old", "status": "pending"}]}')
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute(
            "INSERT INTO messages VALUES (3,'live','tool',?,'todo',1010)",
            ('{"todos": [{"content": "new", "status": "pending"}]}',),
        )
        conn.commit()
        conn.close()
        assert [task.title for task in _collector(tmp_path).collect().tasks] == ["new"]


class TestKanban:
    def test_counts_only_active_work(self, tmp_path: Path) -> None:
        _kanban_db(
            tmp_path,
            "INSERT INTO tasks VALUES ('a','A','in_progress',1,10);"
            "INSERT INTO tasks VALUES ('b','B','todo',1,10);"
            "INSERT INTO tasks VALUES ('c','C','completed',1,10);"
            "INSERT INTO tasks VALUES ('d','D','archived',1,10);",
        )
        assert _collector(tmp_path).collect().kanban_active == 2

    def test_null_titles_are_dropped(self, tmp_path: Path) -> None:
        _kanban_db(tmp_path, "INSERT INTO tasks VALUES ('a',NULL,'in_progress',1,10);")
        assert _collector(tmp_path).collect().tasks == ()

    def test_max_tasks_is_respected(self, tmp_path: Path) -> None:
        rows = "".join(
            f"INSERT INTO tasks VALUES ('k{i}','T{i}','in_progress',1,10);" for i in range(10)
        )
        _kanban_db(tmp_path, rows)
        assert len(_collector(tmp_path, max_tasks=3).collect().tasks) == 3

    def test_works_without_an_updated_at_column(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "kanban.db")
        conn.executescript(
            "CREATE TABLE tasks (title TEXT, status TEXT);"
            "INSERT INTO tasks VALUES ('A','todo');"
        )
        conn.commit()
        conn.close()
        snapshot = _collector(tmp_path).collect()
        assert [task.title for task in snapshot.tasks] == ["A"]

    def test_session_todos_are_not_duplicated_by_kanban(self, tmp_path: Path) -> None:
        _session_db(tmp_path)
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute(
            "INSERT INTO messages VALUES (2,'live','tool',?,'todo',1005)",
            ('{"todos": [{"content": "shared", "status": "pending"}]}',),
        )
        conn.commit()
        conn.close()
        _kanban_db(tmp_path, "INSERT INTO tasks VALUES ('k','shared','todo',1,10);")
        assert [task.title for task in _collector(tmp_path).collect().tasks] == ["shared"]


class TestMemoryFiles:
    def test_profile_size_sums_both_files(self, tmp_path: Path) -> None:
        (tmp_path / "memories").mkdir()
        (tmp_path / "memories" / "MEMORY.md").write_text("12345", encoding="utf-8")
        (tmp_path / "memories" / "USER.md").write_text("678", encoding="utf-8")
        assert _collector(tmp_path).collect().memory.profile_chars == 8

    def test_missing_memory_files_are_not_fatal(self, tmp_path: Path) -> None:
        assert _collector(tmp_path).collect().memory.profile_chars == 0

    def test_non_utf8_memory_file_is_not_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "memories").mkdir()
        (tmp_path / "memories" / "MEMORY.md").write_bytes(b"\xff\xfe\x00binary")
        assert _collector(tmp_path).collect().memory.profile_chars >= 0

    def test_facts_are_counted_and_trust_averaged(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "memory_store.db")
        conn.executescript(FACTS_DDL)
        conn.execute("INSERT INTO facts VALUES (1,'a',0.9,5)")
        conn.execute("INSERT INTO facts VALUES (2,'b',0.7,0)")
        conn.commit()
        conn.close()
        memory = _collector(tmp_path).collect().memory
        assert memory.fact_count == 2
        assert memory.average_trust == 0.8
        assert memory.retrieved_facts == 1


class TestAgentLog:
    def test_missing_log_is_not_fatal(self, tmp_path: Path) -> None:
        assert _collector(tmp_path).collect().recent_events == ()

    def test_binary_log_is_not_fatal(self, tmp_path: Path) -> None:
        _log(tmp_path, "\x00\xff" * 500)
        assert isinstance(_collector(tmp_path).collect().recent_events, tuple)

    def test_unparseable_lines_produce_no_events(self, tmp_path: Path) -> None:
        _log(tmp_path, "{not json at all\n[1,2,3]\n\n")
        assert _collector(tmp_path).collect().recent_events == ()

    def test_tool_events_are_extracted(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "2026-07-24 12:00:01 INFO hermes.tool_executor: tool terminal completed (0.1s)\n",
        )
        assert _collector(tmp_path).collect().recent_events == ("tool terminal completed",)

    def test_error_lines_are_surfaced_and_redacted(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "2026-07-24 12:00:01 ERROR hermes.agent: upstream failed token=supersecret\n",
        )
        events = _collector(tmp_path).collect().recent_events
        assert len(events) == 1
        assert "<redacted>" in events[0]
        assert "supersecret" not in events[0]

    def test_max_events_is_respected(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "".join(
                f"2026-07-24 12:00:{i:02d} INFO hermes.tool_executor: tool t{i} completed (0.1s)\n"
                for i in range(20)
            ),
        )
        assert len(_collector(tmp_path, max_events=3).collect().recent_events) == 3

    def test_newest_events_win(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "2026-07-24 12:00:01 INFO x: tool first completed (0.1s)\n"
            "2026-07-24 12:00:02 INFO x: tool second completed (0.1s)\n",
        )
        assert _collector(tmp_path, max_events=1).collect().recent_events == (
            "tool second completed",
        )


class TestReadOnlyGuarantee:
    """The collector must never be able to mutate Hermes' state."""

    def test_writes_through_the_collector_connection_are_refused(
        self, tmp_path: Path
    ) -> None:
        _session_db(tmp_path)
        with pytest.raises(sqlite3.Error):
            HermesStateCollector._connect(tmp_path / "state.db").execute(
                "INSERT INTO sessions (id) VALUES ('injected')"
            )

    def test_connect_sets_query_only(self, tmp_path: Path) -> None:
        path = tmp_path / "probe.db"
        sqlite3.connect(path).close()
        conn = HermesStateCollector._connect(path)
        try:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        finally:
            conn.close()

    def test_connect_on_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(sqlite3.Error):
            HermesStateCollector._connect(tmp_path / "nope.db")

    def test_collecting_never_modifies_the_source_files(self, tmp_path: Path) -> None:
        _session_db(tmp_path)
        _kanban_db(tmp_path, "INSERT INTO tasks VALUES ('a','A','todo',1,10);")
        log = tmp_path / "logs"
        log.mkdir()
        (log / "agent.log").write_text("2026-07-24 12:00:01 INFO x: tool t completed\n")

        def fingerprint() -> dict[str, bytes]:
            return {
                str(path): path.read_bytes()
                for path in sorted(tmp_path.rglob("*"))
                if path.is_file()
            }

        before = fingerprint()
        _collector(tmp_path).collect()
        assert fingerprint() == before

    def test_locked_database_does_not_raise(self, tmp_path: Path) -> None:
        """A concurrent Hermes transaction must not crash or hang the panel."""
        _session_db(tmp_path)
        blocker = sqlite3.connect(tmp_path / "state.db", timeout=0.1)
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("x", "tui", "m", 1, None, 0, 0, 0, 0, 0, "t", None, 0))
        try:
            snapshot = _collector(tmp_path).collect()
            assert snapshot.session.id == ""
        finally:
            blocker.rollback()
            blocker.close()


class TestSnapshotFromPanel:
    """The legacy PNG renderer consumes snapshots rebuilt from the JSON panel."""

    def test_round_trips_a_collected_snapshot(self, tmp_path: Path) -> None:
        _session_db(tmp_path)
        _kanban_db(tmp_path, "INSERT INTO tasks VALUES ('a','A','todo',1,10);")
        panel = {
            "session": {
                "id": "live", "source": "tui", "model": "gpt-test",
                "title": "ship v1", "status": "working", "current_tool": "none",
                "last_activity": 1000.0, "input_tokens": 10, "output_tokens": 2,
                "context_limit": 1024, "context_percent": 1, "message_count": 1,
                "tool_call_count": 0,
            },
            "tasks": [{"title": "ship v1", "status": "pending", "source": "session"}],
            "kanban_active": 1,
            "memory": {"fact_count": 0, "average_trust": 0.0,
                       "retrieved_facts": 0, "profile_chars": 0},
            "recent_events": ["tool terminal completed"],
        }
        snapshot = DashboardSnapshot.from_panel(panel, "2026-07-24T12:00:00+00:00")
        assert snapshot.session.id == "live"
        assert snapshot.tasks[0].title == "ship v1"
        assert snapshot.recent_events == ("tool terminal completed",)

    @pytest.mark.parametrize(
        "panel",
        [
            {"session": "not a mapping"},
            {"session": {}, "memory": "not a mapping"},
            {"session": {}, "memory": {}, "tasks": "not a sequence"},
            {"session": {}, "memory": {}, "recent_events": 7},
        ],
    )
    def test_rejects_malformed_panels(self, panel: dict) -> None:
        with pytest.raises(ValueError):
            DashboardSnapshot.from_panel(panel, "2026-07-24T12:00:00+00:00")

    def test_unknown_session_keys_are_rejected(self) -> None:
        """Stray keys must not be silently swallowed."""
        panel = {"session": {"surprise": 1}}
        with pytest.raises(TypeError):
            DashboardSnapshot.from_panel(panel, "2026-07-24T12:00:00+00:00")
