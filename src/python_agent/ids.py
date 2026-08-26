"""定义跨模块使用的强类型标识符，避免把不同含义的字符串混用。"""

from __future__ import annotations

from typing import NewType
from uuid import uuid4

SessionId = NewType("SessionId", str)
MessageId = NewType("MessageId", str)
CallId = NewType("CallId", str)
AgentId = SessionId


def new_session_id() -> SessionId:
    return SessionId(str(uuid4()))


def new_message_id() -> MessageId:
    return MessageId(str(uuid4()))


def new_call_id() -> CallId:
    return CallId(str(uuid4()))
