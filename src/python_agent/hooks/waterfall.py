"""通用的异步 Waterfall 中间件链。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

WaterfallHandler = Callable[[Any], Awaitable[Any]]
WaterfallMiddleware = Callable[[Any, WaterfallHandler], Awaitable[Any]]


class Waterfall:
    """按注册顺序执行中间件，并把结果交给下一层或最终处理器。

    每一层都可以在调用 next_handler 前后观察、拒绝或替换结果。工具 Pre、Execute、
    Post 三条流水线共用这个实现；中间件本身不需要知道 Agent Loop 的生命周期。
    """

    def __init__(self, middlewares: Iterable[WaterfallMiddleware] = ()) -> None:
        """复制中间件顺序；链条执行过程中不会读取调用方的原始可变列表。"""

        self._middlewares = list(middlewares)

    def add(self, middleware: WaterfallMiddleware) -> Callable[[], None]:
        """追加一个中间件，并返回可逆的取消注册函数。"""

        self._middlewares.append(middleware)
        removed = False

        def dispose() -> None:
            """幂等地从当前链条移除中间件。"""

            nonlocal removed
            if removed:
                return
            removed = True
            if middleware in self._middlewares:
                self._middlewares.remove(middleware)

        return dispose

    async def run(self, value: Any, terminal: WaterfallHandler) -> Any:
        """从第一层开始执行链路，所有后台工作都由当前调用方 await。

        当前层通过 next_handler 委托给下一层；如果某层不调用 next_handler，就会短路
        后续链路，适合实现拒绝、缓存或结果替换。
        """

        async def dispatch(index: int, current: Any) -> Any:
            """递归进入第 index 层；递归深度由注册的中间件数量决定。"""

            if index >= len(self._middlewares):
                return await terminal(current)
            middleware = self._middlewares[index]

            async def next_handler(next_value: Any) -> Any:
                """把替换后的值交给下一层，保留当前层的包装关系。"""

                return await dispatch(index + 1, next_value)

            return await middleware(current, next_handler)

        return await dispatch(0, value)
