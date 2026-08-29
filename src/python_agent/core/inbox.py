"""Agent 的双队列 Durable Inbox。

Inbox 本身只负责消息排队和操作记录，不负责启动 Agent。每一次插入、领取、替换和删除
都会追加 ``agent/inbox/spliced`` 事件；恢复时重放这些事件即可得到和崩溃前一致的待处理
消息集合。这里的 durable 指事件语义可恢复，磁盘 JSONL 落盘由后续阶段的 SessionStore
负责。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from python_agent.ids import MessageId, new_message_id
from python_agent.session.events import SessionEvent
from python_agent.session.session import Session

InboxQueue = Literal["next_turn", "next_step"]
InboxKind = Literal["followup", "steer", "inject"]


class UserMessage(BaseModel):
    """进入 Inbox 的用户消息，包含稳定的 MessageId 和输入语义。

    kind 不会改变消息最终在模型中的 ``user`` 角色；它只决定消息如何排队、何时唤醒
    Driver，以及在恢复时如何解释原始 Inbox 操作。
    """

    model_config = ConfigDict(extra="forbid")

    message_id: MessageId = Field(default_factory=new_message_id)
    content: str = Field(min_length=1)
    kind: InboxKind = "followup"


def normalize_message(message: str | UserMessage, kind: InboxKind) -> UserMessage:
    """把字符串或 UserMessage 统一成带语义的内部消息。"""

    if isinstance(message, UserMessage):
        return message.model_copy(update={"kind": kind})
    return UserMessage(content=message, kind=kind)


class Inbox:
    """维护 next_turn 和 next_step 两个相互独立的 FIFO 队列。

    队列内容不是模型上下文的平行副本。消息被 claim 后，AgentLoop 会把它写成正式的
    ``user/message`` Session 事件；因此 Inbox 只表示尚未消费的工作。
    """

    def __init__(self, session: Session, *, replay: bool = False) -> None:
        """绑定一个 Session；replay=True 时从已有 spliced 事件恢复未消费消息。"""

        self.session = session
        self._next_turn: deque[UserMessage] = deque()
        self._next_step: deque[UserMessage] = deque()
        if replay:
            self.replay(session.events)

    @property
    def has_next_turn(self) -> bool:
        """是否有等待开启新 Turn 的 followup。"""

        return bool(self._next_turn)

    @property
    def has_next_step(self) -> bool:
        """是否有等待下一模型 Step 的 steer 或 inject。"""

        return bool(self._next_step)

    @property
    def has_pending(self) -> bool:
        """是否存在任意未领取消息，包括不会主动唤醒的 inject。"""

        return self.has_next_turn or self.has_next_step

    @property
    def has_wakeup_pending(self) -> bool:
        """返回是否存在会唤醒 idle Agent 的消息。

        next_turn 中的 followup 总会唤醒；next_step 中只有 steer 会唤醒。inject 的设计是
        静默写入上下文，必须等待其他输入来唤醒 Agent。
        """

        return self.has_next_turn or any(message.kind == "steer" for message in self._next_step)

    def pending(self, queue: InboxQueue | None = None) -> tuple[UserMessage, ...]:
        """返回指定队列的只读快照，便于调试和测试，不暴露内部 deque。"""

        if queue == "next_turn":
            return tuple(self._next_turn)
        if queue == "next_step":
            return tuple(self._next_step)
        return tuple(self._next_turn) + tuple(self._next_step)

    def append(self, message: str | UserMessage, kind: InboxKind) -> MessageId:
        """追加一条消息并持久化 insert 操作。"""

        item = normalize_message(message, kind)
        queue: InboxQueue = "next_turn" if kind == "followup" else "next_step"
        self._queue(queue).append(item)
        self._record("insert", queue, item)
        return item.message_id

    def claim_next_turn(self) -> UserMessage | None:
        """领取一个 next_turn 消息；每个 Turn 只自动领取一条 followup。"""

        if not self._next_turn:
            return None
        return self._claim_from("next_turn", 0)

    def claim_idle_wakeup(self) -> UserMessage | None:
        """领取一条能够唤醒 idle Agent 的消息。

        优先处理 next_turn，保证 followup 开启独立 Turn；没有 followup 时再领取最早的
        steer，让 idle Agent 也能被纠偏消息唤醒。inject 永远不会被这里单独领取。
        """

        item = self.claim_next_turn()
        if item is not None:
            return item
        for index, candidate in enumerate(self._next_step):
            if candidate.kind == "steer":
                return self._claim_from("next_step", index)
        return None

    def claim_next_step(self) -> list[UserMessage]:
        """在 Step 边界领取当前所有 next_step 消息，并保持 FIFO 顺序。"""

        items: list[UserMessage] = []
        while self._next_step:
            items.append(self._claim_from("next_step", 0))
        return items

    def replace(self, message_id: MessageId, message: str | UserMessage) -> bool:
        """替换队列中指定消息的内容，保留原 MessageId 以便恢复和审计。"""

        for queue_name in ("next_turn", "next_step"):
            queue = self._queue(queue_name)
            for index, old in enumerate(queue):
                if old.message_id == message_id:
                    replacement = normalize_message(message, old.kind).model_copy(
                        update={"message_id": message_id}
                    )
                    queue[index] = replacement
                    self._record("replace", queue_name, replacement, replaced_message_id=message_id)
                    return True
        return False

    def delete(self, message_id: MessageId) -> bool:
        """删除一条尚未领取的消息并记录 delete 操作。"""

        for queue_name in ("next_turn", "next_step"):
            queue = self._queue(queue_name)
            for index, item in enumerate(queue):
                if item.message_id == message_id:
                    del queue[index]
                    self._record("delete", queue_name, item)
                    return True
        return False

    def clear(self) -> None:
        """删除两个队列中的所有待处理消息，常用于取消且不保留 Inbox 的场景。"""

        for queue_name in ("next_turn", "next_step"):
            queue = self._queue(queue_name)
            while queue:
                item = queue.popleft()
                self._record("delete", queue_name, item)

    def replay(self, events: Iterable[SessionEvent]) -> None:
        """从 agent/inbox/spliced 事件重建队列，不重新写入事件。"""

        for event in events:
            if event.type != "agent/inbox/spliced":
                continue
            data = event.data
            operation = data.get("operation")
            queue_name = data.get("queue")
            raw_message = data.get("message")
            if queue_name not in {"next_turn", "next_step"} or not isinstance(raw_message, dict):
                continue
            item = UserMessage.model_validate(raw_message)
            if operation == "insert" or operation == "replace":
                self._remove_without_record(item.message_id)
                self._queue(queue_name).append(item)
            elif operation == "claim" or operation == "delete":
                self._remove_without_record(item.message_id)

    def _queue(self, queue_name: InboxQueue) -> deque[UserMessage]:
        """把公开队列名称映射到对应的内部 deque。"""

        return self._next_turn if queue_name == "next_turn" else self._next_step

    def _claim_from(self, queue_name: InboxQueue, index: int) -> UserMessage:
        """按索引领取消息，并记录 claim；rotate 保持 deque 的剩余顺序不变。"""

        queue = self._queue(queue_name)
        queue.rotate(-index)
        item = queue.popleft()
        queue.rotate(index)
        self._record("claim", queue_name, item)
        return item

    def _remove_without_record(self, message_id: MessageId) -> None:
        """仅用于 replay 的内部删除，不追加新的事件，避免恢复过程污染日志。"""

        for queue in (self._next_turn, self._next_step):
            for index, item in enumerate(queue):
                if item.message_id == message_id:
                    del queue[index]
                    return

    def _record(
        self,
        operation: Literal["insert", "claim", "replace", "delete"],
        queue_name: InboxQueue,
        item: UserMessage,
        *,
        replaced_message_id: MessageId | None = None,
    ) -> None:
        """把一次队列变更编码成可重放的 agent/inbox/spliced 事件。"""

        data: dict[str, Any] = {
            "operation": operation,
            "queue": queue_name,
            "message": item.model_dump(mode="json"),
        }
        if replaced_message_id is not None:
            data["replaced_message_id"] = str(replaced_message_id)
        self.session.append("agent/inbox/spliced", data)
