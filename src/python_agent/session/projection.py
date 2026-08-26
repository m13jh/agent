"""把 Session 事件纯函数式地投影为模型可见消息。"""

from __future__ import annotations

import json
from typing import Any

from python_agent.errors import ProjectionError
from python_agent.session.events import SessionEvent


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProjectionError(f"event value is not JSON serializable: {exc}") from exc


def _tool_call(call: dict[str, Any]) -> dict[str, Any]:
    call_id = call.get("id", call.get("call_id"))
    name = call.get("name")
    arguments = call.get("arguments", {})
    if not isinstance(call_id, str) or not call_id:
        raise ProjectionError("assistant tool call has no id")
    if not isinstance(name, str) or not name:
        raise ProjectionError("assistant tool call has no name")
    if not isinstance(arguments, dict):
        raise ProjectionError(f"tool call {call_id} arguments must be an object")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, sort_keys=True),
        },
    }


def derive_messages(events: list[SessionEvent] | tuple[SessionEvent, ...]) -> list[dict[str, Any]]:
    """从只追加事件序列派生稳定的 OpenAI 风格消息。

    控制类事件和未完成的流式 assistant chunk 会被有意忽略。工具调用由发起它的
    ``assistant/message`` 承载；后续 ``tool/result`` 再补充与 call id 对应的 tool 消息。
    这样同一份事件日志在进程重启或回放后仍能产生完全相同的模型请求。
    """

    messages: list[dict[str, Any]] = []
    known_calls: set[str] = set()
    returned_calls: set[str] = set()

    for event in events:
        if event.ignorable:
            continue
        data = event.data
        if event.type == "user/message":
            content = data.get("content", data.get("text"))
            if content is None:
                raise ProjectionError("user/message has no content")
            messages.append({"role": "user", "content": _text(content)})
        elif event.type == "assistant/message":
            content = data.get("content")
            raw_calls = data.get("tool_calls", [])
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ProjectionError("assistant/message tool_calls must be a list")
            calls = [_tool_call(call) for call in raw_calls]
            known_calls.update(call["id"] for call in calls)
            message: dict[str, Any] = {"role": "assistant", "content": content}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
        elif event.type == "tool/call":
            call_id = data.get("call_id", data.get("id"))
            if not isinstance(call_id, str) or not call_id:
                raise ProjectionError("tool/call has no call_id")
            # 这是工具真正开始执行时留下的持久记录。对于手写 fixture 或恢复中的旧日志，
            # 它可能是当前能看到的唯一 call 记录，因此允许它作为 call identity 的兜底来源。
            known_calls.add(call_id)
        elif event.type == "tool/result":
            call_id = data.get("call_id", data.get("id"))
            if not isinstance(call_id, str) or not call_id:
                raise ProjectionError("tool/result has no call_id")
            if call_id not in known_calls:
                raise ProjectionError(f"tool/result {call_id} has no preceding tool call")
            if call_id in returned_calls:
                raise ProjectionError(f"tool/result {call_id} is duplicated")
            returned_calls.add(call_id)
            result = data.get("content", data.get("result"))
            message = {
                "role": "tool",
                "tool_call_id": call_id,
                "name": data.get("name", "unknown_tool"),
                "content": _text(result),
            }
            messages.append(message)
        elif event.type in {
            "turn/start",
            "turn/end",
            "step/start",
            "step/end",
            "assistant/chunk",
            "agent/inbox/spliced",
            "request/header",
            "todo/write",
        }:
            continue
        else:
            # 未知事件只有显式标记 ignorable 才能被忽略。影响上下文的事件如果拼写错误，
            # 必须立即失败，不能静默改变未来的模型请求。
            raise ProjectionError(f"unknown non-ignorable event type: {event.type}")

    return messages


def render_transcript(events: list[SessionEvent] | tuple[SessionEvent, ...]) -> str:
    """把持久事件渲染成紧凑、便于人工阅读和回放的文本 transcript。"""

    lines: list[str] = []
    for event in events:
        if event.type == "user/message":
            lines.append(f"user: {_text(event.data.get('content', ''))}")
        elif event.type == "assistant/message":
            lines.append(f"assistant: {_text(event.data.get('content', ''))}")
        elif event.type == "tool/call":
            lines.append(
                f"tool call {event.data.get('call_id')}: "
                f"{event.data.get('name')}({_text(event.data.get('arguments', {}))})"
            )
        elif event.type == "tool/result":
            lines.append(
                f"tool result {event.data.get('call_id')}: {_text(event.data.get('content'))}"
            )
        elif event.type in {"turn/start", "turn/end", "step/start", "step/end"}:
            lines.append(f"{event.type}: {_text(event.data)}")
    return "\n".join(lines)
