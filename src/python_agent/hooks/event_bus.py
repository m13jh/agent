"""只观察事实、不修改结果的异步 Live Event Bus。"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

EventHandler = Callable[[str, dict[str, Any]], None | Awaitable[None]]


class LiveEventBus:
    """在 Agent、Session、工具和 UI 之间转发实时通知。

    订阅者只接收已经发生的事实，不能替换 Agent 的返回值。emit 会按注册顺序 await
    每一个监听器，因此不会偷偷创建无主的 asyncio Task；返回的 disposer 可以撤销一条
    订阅，适合绑定到 Agent 或应用生命周期。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> Callable[[], None]:
        """订阅指定事件，返回一个幂等的取消订阅函数。"""

        self._handlers[event_type].append(handler)
        removed = False

        def dispose() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return dispose

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """按注册顺序通知指定事件和通配符 ``*`` 监听器。"""

        payload = dict(data or {})
        handlers = [*self._handlers.get(event_type, []), *self._handlers.get("*", [])]
        for handler in handlers:
            result = handler(event_type, payload)
            if inspect.isawaitable(result):
                await result

    def emit_sync(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """在同步的 Session.append 回调中通知同步监听器。

        Session 追加必须保持同步且不能创建无主后台 Task，因此异步监听器不会在这里被
        强行调度。Agent 的异步状态、模型和工具事件仍通过 ``emit`` 完整支持异步监听器。
        """

        payload = dict(data or {})
        handlers = [*self._handlers.get(event_type, []), *self._handlers.get("*", [])]
        for handler in handlers:
            result = handler(event_type, payload)
            if inspect.isawaitable(result):
                result.close() if inspect.iscoroutine(result) else None
