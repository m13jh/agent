"""提供内存中的只追加 Session 事件日志。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from python_agent.errors import SessionError
from python_agent.ids import SessionId, new_session_id
from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.projection import derive_messages, render_transcript

EventWriter = Callable[[SessionEvent], None]


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
        event_writer: EventWriter | None = None,
    ) -> None:
        """创建 Session，并按顺序校验加载的历史事件。

        ``event_writer`` 是持久化提交边界：新事件必须先由 writer 成功写入稳定存储，
        才能进入 ``events`` 内存列表。加载已有事件时不会调用 writer，否则一次恢复会把
        整份历史重复追加到 JSONL。纯内存 Session 不传 writer，行为与前三阶段完全一致。
        """

        self.header = header
        self.event_listener = event_listener
        self._event_writer = event_writer
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

        序号来自当前事件列表长度，因而新事件只能追加到末尾。如果 Session 绑定了
        JSONL Store，writer 会先完成写入、flush 与 fsync；只有持久化成功后才更新内存，
        从而落实“落盘是真相源”的顺序。监听器最后收到的也必然是已提交事实。
        """

        event = SessionEvent(
            seq=len(self.events),
            type=event_type,
            data=data or {},
            source_event_seqs=source_event_seqs,
            ignorable=ignorable,
        )
        if self._event_writer is not None:
            # writer 异常必须原样向上传递，不能把事件只留在内存里继续运行；否则进程
            # 重启后的模型上下文会与当前进程已经看见的上下文发生分叉。
            self._event_writer(event)
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

    def set_event_writer(self, writer: EventWriter | None) -> None:
        """绑定或移除事件持久化 writer。

        该入口主要供 ``SessionStore`` 使用。调用方应只在 Session 尚未并发运行时切换
        writer；Agent 的单 Driver 规则保证正常运行期间所有 append 都在同一事件循环线程
        内串行发生。
        """

        self._event_writer = writer

    def messages(self) -> list[dict[str, Any]]:
        """根据完整事件列表重新计算模型消息，不维护第二份消息副本。"""

        return derive_messages(self.events)

    def transcript(self) -> str:
        """生成当前 Session 的人工可读 transcript。"""

        return render_transcript(self.events)

    def export_transcript(self, path: Path) -> Path:
        """把当前事件投影原子导出为 UTF-8 transcript 文件。

        先在目标目录写临时文件并 fsync，再使用 ``os.replace`` 原子替换目标。这样即使
        导出过程中进程退出，调用方也只会看到旧的完整文件或新的完整文件，而不会看到
        半截 transcript。导出内容仍由事件日志即时投影，不维护额外文本副本。
        """

        destination = path.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(self.transcript())
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, destination)
        except OSError:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise
        return destination

    def next_turn(self) -> int:
        """根据已有 turn/start 事件计算下一个 Turn 编号。"""

        starts = [event.data.get("turn", 0) for event in self.events if event.type == "turn/start"]
        return max(starts, default=0) + 1

    def next_step(self) -> int:
        """根据已有 step/start 事件计算下一个全局 Step 编号。"""

        starts = [event.data.get("step", 0) for event in self.events if event.type == "step/start"]
        return max(starts, default=0) + 1
