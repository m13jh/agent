"""工具审批服务抽象及其常用实现。"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from python_agent.ids import CallId


class ApprovalRequest(BaseModel):
    """提交给用户或上层策略的审批请求，不包含 API Key 等环境秘密。"""

    model_config = ConfigDict(extra="forbid")

    call_id: CallId = Field(description="需要审批的工具调用 ID")
    tool_name: str = Field(description="需要审批的工具名称")
    arguments: dict[str, Any] = Field(description="工具即将收到的参数")
    reason: str = Field(description="要求审批的原因")


@runtime_checkable
class ApprovalService(Protocol):
    """高风险工具调用的最小异步审批接口。"""

    async def request(self, approval: ApprovalRequest) -> bool:
        """返回是否允许本次具体工具调用；拒绝应当是默认安全结果。"""

        ...


class DenyApprovalService:
    """默认拒绝服务：没有明确审批能力时 fail closed。"""

    async def request(self, approval: ApprovalRequest) -> bool:
        """拒绝审批；参数只为满足统一接口而传入，不会被记录或打印。"""

        del approval
        return False


ApprovalCallback = Callable[[ApprovalRequest], bool | Awaitable[bool]]


class CallbackApprovalService:
    """把审批交给应用层回调，适用于 CLI、UI 或测试。"""

    def __init__(self, callback: ApprovalCallback) -> None:
        """保存应用层回调；回调的同步/异步差异在 request 中统一处理。"""

        self.callback = callback

    async def request(self, approval: ApprovalRequest) -> bool:
        """执行应用层回调，并把同步或异步返回值统一转换成 bool。"""

        result = self.callback(approval)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)


ApprovalNotice = Callable[[ApprovalRequest], None]


class InteractiveApprovalService:
    """把审批请求排队交给交互层，并等待用户显式回答。

    Agent Driver 在等待审批时不能再读取 stdin；因此这个服务只负责保存 Future 和通知
    UI，具体的 ``y``/``n`` 输入由 CLI 的主输入循环调用 :meth:`respond` 路由回来。这样
    全屏 prompt_toolkit 和纯文本 PromptSession 都只保留一个 stdin 所有者。
    """

    def __init__(self, on_request: ApprovalNotice) -> None:
        """创建交互审批队列；通知回调不得阻塞或自行读取 stdin。"""

        self.on_request = on_request
        self._queue: deque[tuple[ApprovalRequest, asyncio.Future[bool]]] = deque()
        self._active: tuple[ApprovalRequest, asyncio.Future[bool]] | None = None
        self._closed = False

    @property
    def pending_request(self) -> ApprovalRequest | None:
        """返回当前需要用户处理的请求；没有请求时返回 None。"""

        return self._active[0] if self._active is not None else None

    @property
    def has_pending(self) -> bool:
        """是否存在当前显示给用户的待审批请求。"""

        return self._active is not None

    async def request(self, approval: ApprovalRequest) -> bool:
        """排队一个请求并等待 :meth:`respond`；关闭或取消时均按拒绝处理。"""

        if self._closed:
            return False
        future = asyncio.get_running_loop().create_future()
        self._queue.append((approval, future))
        self._activate_next()
        try:
            return bool(await future)
        except asyncio.CancelledError:
            self._remove_future(future)
            raise

    def _activate_next(self) -> None:
        """让队首请求成为 active，并通知 UI 一次。"""

        if self._active is not None or not self._queue or self._closed:
            return
        self._active = self._queue.popleft()
        request = self._active[0]
        try:
            self.on_request(request)
        except Exception:
            # UI 通知不是安全边界；renderer 出错时仍然必须拒绝本次操作并继续收敛队列。
            self.respond(False)

    def _remove_future(self, future: asyncio.Future[bool]) -> None:
        """从 active 或等待队列移除已取消的审批 Future。"""

        if self._active is not None and self._active[1] is future:
            self._active = None
            self._activate_next()
            return
        self._queue = deque(item for item in self._queue if item[1] is not future)

    def respond(self, approved: bool) -> bool:
        """提交当前请求的用户决定；返回是否确实消费了一个请求。"""

        if self._active is None:
            return False
        _request, future = self._active
        self._active = None
        if not future.done():
            future.set_result(bool(approved))
        self._activate_next()
        return True

    def close(self) -> None:
        """关闭队列并拒绝所有未完成请求，供 chat 退出时调用。"""

        self._closed = True
        if self._active is not None:
            _request, future = self._active
            self._active = None
            if not future.done():
                future.set_result(False)
        while self._queue:
            _request, future = self._queue.popleft()
            if not future.done():
                future.set_result(False)
