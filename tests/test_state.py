import json
import sqlite3
from pathlib import Path

from hermes_eink_dashboard.state import DashboardConfig, HermesStateCollector


def create_db(path: Path, statements: list[str]) -> None:
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.executescript(statement)
    conn.commit()
    conn.close()


def test_collects_live_hermes_state_without_exposing_tool_output(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    create_db(state_db, [
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, ended_at REAL,
            message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
            output_tokens INTEGER, cache_read_tokens INTEGER, title TEXT, display_name TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_name TEXT, timestamp REAL
        );
        INSERT INTO sessions VALUES
          ('old', 'tui', 'old-model', 100, 200, 2, 0, 100, 10, 0, 'Old work', NULL, 0),
          ('live', 'tui', 'gpt-test', 900, NULL, 8, 3, 999999, 2048, 60000,
           'Sensitive prompt title', NULL, 0);
        INSERT INTO messages VALUES
          (1, 'old', 'user', 'old', NULL, 150),
          (2, 'live', 'user', 'build the dashboard', NULL, 1000),
          (3, 'live', 'tool', '{"secret":"must not leak"}', 'terminal', 1010),
          (4, 'live', 'tool', '{"todos":[{"id":"one","content":"Render PNG","status":"in_progress"},{"id":"two","content":"Ship","status":"pending"}]}', 'todo', 1020),
          (5, 'live', 'user', '[Your active task list was preserved across context compression]', NULL, 1025);
        """
    ])

    kanban_db = tmp_path / "kanban.db"
    create_db(kanban_db, [
        """
        CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, priority INTEGER, updated_at REAL);
        INSERT INTO tasks VALUES ('k1', 'Review dashboard', 'in_progress', 2, 1000);
        INSERT INTO tasks VALUES ('k2', 'Completed work', 'completed', 1, 900);
        """
    ])

    memory_db = tmp_path / "memory_store.db"
    create_db(memory_db, [
        """
        CREATE TABLE facts (fact_id INTEGER, trust_score REAL, retrieval_count INTEGER);
        INSERT INTO facts VALUES (1, 0.9, 4), (2, 0.5, 0);
        """
    ])

    memory_md = tmp_path / "MEMORY.md"
    user_md = tmp_path / "USER.md"
    memory_md.write_text("memory facts")
    user_md.write_text("user facts")
    log_path = tmp_path / "agent.log"
    log_path.write_text(
        "2026-07-24 12:00:00 INFO agent.conversation_loop: API call #1: "
        "profile=default model=gpt-test provider=test in=65536 out=20 total=65556 latency=1.2s\n"
        "2026-07-24 12:00:01 INFO agent.tool_executor: tool terminal completed (0.2s, 99 chars)\n"
    )

    config = DashboardConfig(
        hermes_home=tmp_path,
        state_db=state_db,
        kanban_db=kanban_db,
        memory_db=memory_db,
        memory_file=memory_md,
        user_file=user_md,
        agent_log=log_path,
        context_limit=262144,
    )
    snapshot = HermesStateCollector(config, now=lambda: 1030).collect()

    assert snapshot.session.id == "live"
    assert snapshot.session.model == "gpt-test"
    assert snapshot.session.title == "Render PNG"
    assert snapshot.session.current_tool == "todo"
    assert snapshot.session.context_percent == 25
    assert [task.title for task in snapshot.tasks][:2] == ["Render PNG", "Ship"]
    assert snapshot.kanban_active == 1
    assert snapshot.memory.fact_count == 2
    assert snapshot.memory.profile_chars == len("memory facts") + len("user facts")
    serialized = json.dumps(snapshot.to_dict())
    assert "must not leak" not in serialized
    assert "build the dashboard" not in serialized
    assert "Sensitive prompt title" not in serialized
    assert "tool terminal completed" in serialized
