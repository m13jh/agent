"""Session 的只追加事件日志，以及从事件派生的消息和 transcript 投影。

Session 模块只负责记录和投影，不负责调用模型或执行工具，这样日志可以独立回放。
"""

from python_agent.session.compaction import (
    CallbackSummaryProvider,
    ContextCompactor,
    StaticSummaryProvider,
    SummaryProvider,
)
from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.jsonl_store import JsonlSessionStore, SessionRepairReport
from python_agent.session.projection import derive_messages
from python_agent.session.session import Session
from python_agent.session.sqlite_index import SessionSearchHit, SqliteSessionIndex
from python_agent.session.store import SessionStore

__all__ = [
    "JsonlSessionStore",
    "CallbackSummaryProvider",
    "ContextCompactor",
    "Session",
    "SessionEvent",
    "SessionHeader",
    "SessionRepairReport",
    "SessionStore",
    "SessionSearchHit",
    "SqliteSessionIndex",
    "StaticSummaryProvider",
    "SummaryProvider",
    "derive_messages",
]
