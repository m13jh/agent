"""提供内存中的只追加 Session 事件日志。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from python_agent.errors import SessionError
from python_agent.ids import SessionId, new_session_id
from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.projection import derive_messages, render_transcript


class Session:
    """一次 Agent 运行的权威事件日志。

    该对象只把事件列表作为可变状态；messages 和 transcript 都按需重新计算，避免维护
    平行副本，从而保证恢复和 replay 的行为是确定性的。
    """

    def __init__(
        self,
        header: SessionHeader,
        events: Iterable[SessionEvent] = (),
        event_listener: Callable[[SessionEvent], None] | None = None,
    ) -> None:
        """创建 Session，并按顺序校验加载的历史事件。"""

        self.header = header
        self.event_listener = event_listener
        self.events: list[SessionEvent] = []
        for event in events:
            self._append_existing(event)

    @classmethod
    def new(
        cls,
        *,
        session_id: SessionId | None = None,
        cwd: Any = None,
        agent_preset: str | None = None,
    ) -> Session:
        """创建带新 ID 和 Header 的空 Session。"""

        header = SessionHeader(
            id=session_id or new_session_id(),
            cwd=cwd,
            agent_preset=agent_preset,
        )
        return cls(header)

    @property
    def id(self) -> SessionId:
        """返回 Header 中稳定的 Session ID，Agent ID 与它共用同一值。"""

        return self.header.id

    def _append_existing(self, event: SessionEvent) -> None:
        """加载历史事件时验证 seq 等于当前长度，拒绝断裂或重复日志。"""

        expected = len(self.events)
        if event.seq != expected:
            raise SessionError(
                f"event sequence must be contiguous: expected {expected}, got {event.seq}"
            )
        self.events.append(event)

    def append(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        source_event_seqs: list[int] | None = None,
        ignorable: bool = False,
    ) -> SessionEvent:
        """追加一条事件并分配连续序号。

        序号来自当前事件列表长度，因而新事件只能追加到末尾。事件对象加入内存日志
        后才通知观察器，监听器看到的必然是已经成功提交的事实。
        """

        event = SessionEvent(
            seq=len(self.events),
            type=event_type,
            data=data or {},
            source_event_seqs=source_event_seqs,
            ignorable=ignorable,
        )
        self.events.append(event)
        if self.event_listener is not None:
            self.event_listener(event)
        return event

    def set_event_listener(self, listener: Callable[[SessionEvent], None] | None) -> None:
        """设置实时事件观察器。

        监听器只接收已经追加成功的事件，不允许替换或阻止事件，因此不会改变 Session
        作为权威事实源的角色。持久化和回放时可以不设置监听器。
        """

        self.event_listener = listener

    def messages(self) -> list[dict[str, Any]]:
        """根据完整事件列表重新计算模型消息，不维护第二份消息副本。"""

        return derive_messages(self.events)

    def transcript(self) -> str:
        """生成当前 Session 的人工可读 transcript。"""

        return render_transcript(self.events)

    def next_turn(self) -> int:
        """根据已有 turn/start 事件计算下一个 Turn 编号。"""

        starts = [event.data.get("turn", 0) for event in self.events if event.type == "turn/start"]
        return max(starts, default=0) + 1

    def next_step(self) -> int:
        """根据已有 step/start 事件计算下一个全局 Step 编号。"""

        starts = [event.data.get("step", 0) for event in self.events if event.type == "step/start"]
        return max(starts, default=0) + 1
