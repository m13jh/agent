"""从 JSONL 真相源重建的 SQLite 跨 Session 查询索引。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from python_agent.ids import SessionId
from python_agent.session.events import SessionEvent
from python_agent.session.session import Session
from python_agent.session.store import SessionStore


class SessionSearchHit(BaseModel):
    """一条跨 Session 文本命中。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: SessionId
    seq: int = Field(ge=0)
    event_type: str
    role: str | None = None
    snippet: str


class SqliteSessionIndex:
    """可随时删除重建的查询索引；绝不替代 events.jsonl 权威日志。"""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        """创建数据库目录、启用外键，并初始化稳定 schema。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                cwd TEXT,
                agent_preset TEXT,
                parent_session_id TEXT,
                forked_from_session_id TEXT,
                origin TEXT NOT NULL,
                delegation_depth INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS searchable_events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                role TEXT,
                content TEXT NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (session_id, seq),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_searchable_events_content
                ON searchable_events(content);
            CREATE INDEX IF NOT EXISTS idx_searchable_events_type
                ON searchable_events(event_type);
            """
        )
        return connection

    @staticmethod
    def _searchable(event: SessionEvent) -> tuple[str | None, str] | None:
        """把模型可见或审计有价值的事件提取为纯文本。"""

        data = event.data
        if event.type == "user/message":
            return "user", str(data.get("content", ""))
        if event.type == "assistant/message":
            content = data.get("content")
            calls = data.get("tool_calls", [])
            text = str(content or "")
            if calls:
                text += "\n" + json.dumps(calls, ensure_ascii=False, sort_keys=True)
            return "assistant", text
        if event.type == "tool/result":
            return "tool", json.dumps(data.get("content"), ensure_ascii=False, sort_keys=True)
        if event.type == "tool/call":
            return "tool_call", json.dumps(
                {"name": data.get("name"), "arguments": data.get("arguments", {})},
                ensure_ascii=False,
                sort_keys=True,
            )
        if event.type == "context/summary":
            return "summary", str(data.get("content", ""))
        return None

    @staticmethod
    def _insert_session(connection: sqlite3.Connection, session: Session) -> None:
        """在当前事务中替换一个 Session Header 及其可搜索事件。"""

        header = session.header
        connection.execute("DELETE FROM sessions WHERE id = ?", (str(session.id),))
        connection.execute(
            """
            INSERT INTO sessions (
                id, created_at, cwd, agent_preset, parent_session_id,
                forked_from_session_id, origin, delegation_depth
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(header.id),
                header.created_at.isoformat(),
                str(header.cwd) if header.cwd is not None else None,
                header.agent_preset,
                str(header.parent_session_id) if header.parent_session_id is not None else None,
                (
                    str(header.forked_from_session_id)
                    if header.forked_from_session_id is not None
                    else None
                ),
                header.origin,
                header.delegation_depth,
            ),
        )
        rows: list[tuple[Any, ...]] = []
        for event in session.events:
            searchable = SqliteSessionIndex._searchable(event)
            if searchable is None:
                continue
            role, content = searchable
            if not content:
                continue
            rows.append(
                (
                    str(session.id),
                    event.seq,
                    event.type,
                    role,
                    content,
                    event.model_dump_json(),
                )
            )
        connection.executemany(
            """
            INSERT INTO searchable_events (
                session_id, seq, event_type, role, content, event_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def index_session(self, session: Session) -> None:
        """增量替换一个 Session 的派生索引内容。"""

        with closing(self._connect()) as connection, connection:
            self._insert_session(connection, session)

    async def rebuild(self, store: SessionStore) -> int:
        """严格加载所有 JSONL Session，并在一个事务中重建完整索引。"""

        headers = await store.list()
        sessions = [await store.load(header.id) for header in headers]
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM searchable_events")
            connection.execute("DELETE FROM sessions")
            for session in sessions:
                self._insert_session(connection, session)
        return len(sessions)

    @staticmethod
    def _escape_like(query: str) -> str:
        """转义 LIKE 通配符，让用户查询按字面文本匹配。"""

        return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _snippet(content: str, query: str, *, radius: int = 100) -> str:
        """返回命中附近的紧凑片段。"""

        position = content.casefold().find(query.casefold())
        if position < 0:
            return content[: radius * 2]
        start = max(0, position - radius)
        end = min(len(content), position + len(query) + radius)
        prefix = "…" if start else ""
        suffix = "…" if end < len(content) else ""
        return prefix + content[start:end] + suffix

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        session_id: SessionId | None = None,
    ) -> list[SessionSearchHit]:
        """跨 Session 搜索字面文本，可选择限制到一个 Session。"""

        if not query:
            raise ValueError("search query must be non-empty")
        if limit <= 0 or limit > 500:
            raise ValueError("search limit must be between 1 and 500")
        pattern = f"%{self._escape_like(query)}%"
        sql = (
            "SELECT session_id, seq, event_type, role, content "
            "FROM searchable_events WHERE content LIKE ? ESCAPE '\\' COLLATE NOCASE"
        )
        parameters: list[Any] = [pattern]
        if session_id is not None:
            sql += " AND session_id = ?"
            parameters.append(str(session_id))
        sql += " ORDER BY session_id, seq LIMIT ?"
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            SessionSearchHit(
                session_id=SessionId(row[0]),
                seq=row[1],
                event_type=row[2],
                role=row[3],
                snippet=self._snippet(row[4], query),
            )
            for row in rows
        ]


__all__ = ["SessionSearchHit", "SqliteSessionIndex"]
