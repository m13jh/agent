"""只观察事实、不修改结果的异步 Live Event Bus。"""

from __future__ import annotations

import asyncio
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

    def __init__(self, *, observer_timeout_seconds: float = 5.0) -> None:
        """创建空订阅表。

        观察者只负责诊断或界面展示，不拥有 Agent 正确性的控制权。每个 handler 都会被隔离
        并设置时限，损坏或卡住的渲染器不能阻止 Driver 回到 idle。将时限设为 ``0`` 可以
        为明确受信任的进程内观察者关闭超时限制。
        """

        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        if observer_timeout_seconds < 0:
            raise ValueError("observer_timeout_seconds must be non-negative")
        self.observer_timeout_seconds = observer_timeout_seconds
        self._observer_errors: list[dict[str, Any]] = []

    @property
    def observer_errors(self) -> tuple[dict[str, Any], ...]:
        """返回观察者失败的快照，不重新发送可能递归的事件。"""

        return tuple(dict(error) for error in self._observer_errors)

    def _record_observer_error(
        self,
        event_type: str,
        handler: EventHandler,
        exc: BaseException,
    ) -> None:
        """保存有界诊断信息，同时不保存原始事件负载。"""

        if len(self._observer_errors) >= 256:
            del self._observer_errors[:64]
        handler_name = getattr(handler, "__qualname__", None)
        if not isinstance(handler_name, str):
            handler_name = type(handler).__qualname__
        self._observer_errors.append(
            {
                "event_type": event_type,
                "handler": handler_name,
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
            }
        )

    def subscribe(self, event_type: str, handler: EventHandler) -> Callable[[], None]:
        """订阅指定事件，返回一个幂等的取消订阅函数。"""

        self._handlers[event_type].append(handler)
        removed = False

        def dispose() -> None:
            """幂等地删除这一次订阅，不影响同一事件的其他监听器。"""

            nonlocal removed
            if removed:
                return
            removed = True
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return dispose

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """按注册顺序通知指定事件和通配符 ``*`` 监听器。

        每个异步监听器都会被 await，保证事件处理完成后生命周期操作才继续，同时不
        创建未被任何所有者追踪的后台任务。
        """

        payload = dict(data or {})
        handlers = [*self._handlers.get(event_type, []), *self._handlers.get("*", [])]
        for handler in handlers:
            try:
                result = handler(event_type, payload)
                if inspect.isawaitable(result):
                    if self.observer_timeout_seconds:
                        await asyncio.wait_for(
                            result,
                            timeout=self.observer_timeout_seconds,
                        )
                    else:
                        await result
            except asyncio.CancelledError:
                # 保留 Agent/Driver 自身的取消语义。普通异常会在下面被隔离；外部取消仍必须
                # 让所有者退出，以便执行自身的清理流程。
                raise
            except Exception as exc:
                self._record_observer_error(event_type, handler, exc)

    def emit_sync(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """在同步的 Session.append 回调中通知同步监听器。

        Session 追加必须保持同步且不能创建无主后台 Task，因此异步监听器不会在这里被
        强行调度。Agent 的异步状态、模型和工具事件仍通过 ``emit`` 完整支持异步监听器。
        """

        payload = dict(data or {})
        handlers = [*self._handlers.get(event_type, []), *self._handlers.get("*", [])]
        for handler in handlers:
            try:
                result = handler(event_type, payload)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    self._record_observer_error(
                        event_type,
                        handler,
                        RuntimeError("async observer cannot run from emit_sync"),
                    )
            except Exception as exc:
                self._record_observer_error(event_type, handler, exc)
