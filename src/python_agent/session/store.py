"""Session 持久化后端的稳定接口。

Agent 核心只依赖本模块中的协议，不需要知道事件最终存放在 JSONL、SQLite 还是远程服务。
阶段四提供 ``JsonlSessionStore``；后续实现其他后端时应保持相同的创建、加载与追加语义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from python_agent.ids import SessionId
from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.session import Session


@runtime_checkable
class SessionStore(Protocol):
    """只追加 Session Store 的异步能力边界。

    Store 返回的 ``Session`` 必须已经绑定“先落盘、后入内存”的事件 writer。协议方法使用
    async，是为了让文件、数据库或网络后端拥有统一调用方式；Session 自身的 append 仍然
    保持同步，确保一个事件提交过程不可被 Agent Loop 的其他协程插入。
    """

    async def create(self, header: SessionHeader) -> Session:
        """创建空 Session；同名 ID 已存在时必须拒绝，不能覆盖历史。"""

        ...

    async def load(self, session_id: SessionId, *, repair: bool = False) -> Session:
        """加载并校验 Session；repair=True 时允许修复明确可恢复的崩溃尾部。"""

        ...

    async def append(self, session_id: SessionId, event: SessionEvent) -> None:
        """直接向指定 Session 追加一条已经分配序号的事件。"""

        ...

    async def list(self) -> list[SessionHeader]:
        """返回存储中所有有效 Session Header 的稳定排序快照。"""

        ...

    async def fork(self, source_id: SessionId, target_id: SessionId) -> Session:
        """复制来源事件历史，创建具有新 ID 的独立 Session。"""

        ...

    async def export_transcript(self, session_id: SessionId, path: Path) -> Path:
        """加载 Session 并把可重放 transcript 原子导出到指定文件。"""

        ...


__all__ = ["SessionStore"]
