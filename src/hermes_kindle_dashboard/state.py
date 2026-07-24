from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class DashboardConfig:
    hermes_home: Path
    state_db: Path
    kanban_db: Path
    memory_db: Path
    memory_file: Path
    user_file: Path
    agent_log: Path
    context_limit: int = 262_144
    max_tasks: int = 6
    max_events: int = 5

    @classmethod
    def from_home(cls, hermes_home: Path, context_limit: int = 262_144) -> "DashboardConfig":
        home = hermes_home.expanduser().resolve()
        return cls(
            hermes_home=home,
            state_db=home / "state.db",
            kanban_db=home / "kanban.db",
            memory_db=home / "memory_store.db",
            memory_file=home / "memories" / "MEMORY.md",
            user_file=home / "memories" / "USER.md",
            agent_log=home / "logs" / "agent.log",
            context_limit=context_limit,
        )


@dataclass(frozen=True)
class SessionState:
    id: str = ""
    source: str = "idle"
    model: str = "unknown"
    title: str = "No active session"
    status: str = "idle"
    current_tool: str = "none"
    last_activity: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    context_limit: int = 0
    context_percent: int = 0
    message_count: int = 0
    tool_call_count: int = 0


@dataclass(frozen=True)
class TaskState:
    title: str
    status: str
    source: str


@dataclass(frozen=True)
class MemoryState:
    fact_count: int = 0
    average_trust: float = 0.0
    retrieved_facts: int = 0
    profile_chars: int = 0


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: str
    session: SessionState
    tasks: tuple[TaskState, ...]
    kanban_active: int
    memory: MemoryState
    recent_events: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


class HermesStateCollector:
    """Read-only collector for Hermes' public runtime artifacts.

    The collector intentionally avoids importing Hermes internals. That keeps the
    dashboard compatible across upgrades and prevents it from mutating agent state.
    """

    _LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} [0-9:,]+\s+(INFO|WARNING|ERROR)\s+")
    _TOOL_EVENT = re.compile(r"tool ([A-Za-z0-9_:.-]+) (completed|returned error)", re.I)
    _API_EVENT = re.compile(
        r"API call #\d+:.*?model=([^ ]+).*?provider=([^ ]+).*?in=(\d+).*?out=(\d+).*?latency=([0-9.]+s)",
        re.I,
    )

    def __init__(self, config: DashboardConfig, now: Callable[[], float] | None = None):
        self.config = config
        self._now = now or (lambda: datetime.now(tz=timezone.utc).timestamp())

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    @staticmethod
    def _clean_text(value: str, limit: int = 100) -> str:
        text = re.sub(r"\s+", " ", value or "").strip()
        text = re.sub(r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=<redacted>", text)
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _latest_session(self) -> SessionState:
        if not self.config.state_db.exists():
            return SessionState(context_limit=self.config.context_limit)
        try:
            with self._connect(self.config.state_db) as conn:
                row = conn.execute(
                    """
                    SELECT s.id, s.source, s.model, s.started_at, s.ended_at,
                           s.message_count, s.tool_call_count, s.input_tokens,
                           s.output_tokens,
                           (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id=s.id) AS last_activity
                    FROM sessions s
                    WHERE COALESCE(s.archived, 0)=0
                    ORDER BY last_activity DESC
                    LIMIT 1
                    """
                ).fetchone()
                if not row:
                    return SessionState(context_limit=self.config.context_limit)
                latest_tool = conn.execute(
                    "SELECT tool_name FROM messages WHERE session_id=? AND tool_name IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return SessionState(context_limit=self.config.context_limit)

        last_activity = float(row["last_activity"] or row["started_at"] or 0)
        age = max(0.0, self._now() - last_activity)
        status = "working" if row["ended_at"] is None and age <= 300 else "idle"
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        latest_api = self._latest_api_usage(str(row["model"] or "unknown"))
        if latest_api:
            # sessions.input_tokens is cumulative across API calls; the latest
            # structured API log record is the actual active request/context size.
            input_tokens, output_tokens = latest_api
        limit = max(1, int(self.config.context_limit))
        return SessionState(
            id=str(row["id"] or ""),
            source=str(row["source"] or "unknown"),
            model=str(row["model"] or "unknown"),
            title="Active Hermes session",
            status=status,
            current_tool=str(latest_tool["tool_name"] if latest_tool else "none"),
            last_activity=last_activity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_limit=limit,
            context_percent=min(100, round((input_tokens / limit) * 100)),
            message_count=int(row["message_count"] or 0),
            tool_call_count=int(row["tool_call_count"] or 0),
        )

    def _latest_api_usage(self, model: str) -> tuple[int, int] | None:
        try:
            lines = self.config.agent_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for raw in reversed(lines[-1000:]):
            match = self._API_EVENT.search(self._LOG_PREFIX.sub("", raw))
            if match and match.group(1) == model:
                return int(match.group(3)), int(match.group(4))
        return None

    def _todo_tasks(self, session_id: str) -> list[TaskState]:
        if not session_id or not self.config.state_db.exists():
            return []
        try:
            with self._connect(self.config.state_db) as conn:
                rows = conn.execute(
                    "SELECT content FROM messages WHERE session_id=? AND tool_name='todo' ORDER BY id DESC LIMIT 5",
                    (session_id,),
                ).fetchall()
        except (sqlite3.Error, OSError):
            return []
        for row in rows:
            try:
                payload = json.loads(row["content"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            todos = payload.get("todos") if isinstance(payload, dict) else None
            if not isinstance(todos, list):
                continue
            tasks = []
            for item in todos:
                if not isinstance(item, dict) or item.get("status") in {"completed", "cancelled"}:
                    continue
                title = self._clean_text(str(item.get("content") or ""), 90)
                if title:
                    tasks.append(TaskState(title, str(item.get("status") or "pending"), "session"))
            return tasks[: self.config.max_tasks]
        return []

    def _kanban_tasks(self) -> tuple[list[TaskState], int]:
        if not self.config.kanban_db.exists():
            return [], 0
        try:
            with self._connect(self.config.kanban_db) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
                order = "updated_at DESC" if "updated_at" in columns else "created_at DESC" if "created_at" in columns else "rowid DESC"
                rows = conn.execute(
                    f"SELECT title, status FROM tasks WHERE status NOT IN ('completed','cancelled','archived') ORDER BY {order} LIMIT ?",
                    (self.config.max_tasks,),
                ).fetchall()
                count = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('completed','cancelled','archived')"
                ).fetchone()[0]
        except (sqlite3.Error, OSError):
            return [], 0
        tasks = [
            TaskState(self._clean_text(str(row["title"] or ""), 90), str(row["status"] or "pending"), "kanban")
            for row in rows
            if row["title"]
        ]
        return tasks, int(count)

    def _memory_state(self) -> MemoryState:
        fact_count = retrieved = 0
        average_trust = 0.0
        if self.config.memory_db.exists():
            try:
                with self._connect(self.config.memory_db) as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, COALESCE(AVG(trust_score),0) AS trust, "
                        "SUM(CASE WHEN retrieval_count > 0 THEN 1 ELSE 0 END) AS retrieved FROM facts"
                    ).fetchone()
                    fact_count = int(row["n"] or 0)
                    average_trust = round(float(row["trust"] or 0), 2)
                    retrieved = int(row["retrieved"] or 0)
            except (sqlite3.Error, OSError):
                pass
        profile_chars = 0
        for path in (self.config.memory_file, self.config.user_file):
            try:
                profile_chars += len(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return MemoryState(fact_count, average_trust, retrieved, profile_chars)

    def _recent_events(self) -> tuple[str, ...]:
        try:
            lines = self.config.agent_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ()
        events: list[str] = []
        for raw in reversed(lines[-500:]):
            body = self._LOG_PREFIX.sub("", raw)
            tool = self._TOOL_EVENT.search(body)
            api = self._API_EVENT.search(body)
            if tool:
                events.append(f"tool {tool.group(1)} {tool.group(2).lower()}")
            elif api:
                events.append(
                    f"{api.group(1)} via {api.group(2)} · {api.group(3)} in / {api.group(4)} out · {api.group(5)}"
                )
            elif " ERROR " in raw or " WARNING " in raw:
                safe = self._clean_text(body, 110)
                if safe:
                    events.append(safe)
            if len(events) >= self.config.max_events:
                break
        return tuple(reversed(events))

    def collect(self) -> DashboardSnapshot:
        session = self._latest_session()
        tasks = self._todo_tasks(session.id)
        kanban_tasks, kanban_active = self._kanban_tasks()
        for task in kanban_tasks:
            if len(tasks) >= self.config.max_tasks:
                break
            if task.title not in {existing.title for existing in tasks}:
                tasks.append(task)
        if tasks:
            session = replace(session, title=tasks[0].title)
        generated = datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat()
        return DashboardSnapshot(
            generated_at=generated,
            session=session,
            tasks=tuple(tasks),
            kanban_active=kanban_active,
            memory=self._memory_state(),
            recent_events=self._recent_events(),
        )
