"""定义跨模块使用的强类型标识符，避免把不同含义的字符串混用。"""

from __future__ import annotations

from typing import NewType
from uuid import uuid4

SessionId = NewType("SessionId", str)
MessageId = NewType("MessageId", str)
CallId = NewType("CallId", str)
AgentId = SessionId


def new_session_id() -> SessionId:
    """生成一个新的 Session ID；UUID 保证不同 Agent 的日志可安全区分。"""

    return SessionId(str(uuid4()))


def new_message_id() -> MessageId:
    """生成一个新的消息 ID，用于 Inbox、Session 和工具上下文之间的关联。"""

    return MessageId(str(uuid4()))


def new_call_id() -> CallId:
    """生成一个新的工具调用 ID，用于 assistant tool call 与 tool result 配对。"""

    return CallId(str(uuid4()))
