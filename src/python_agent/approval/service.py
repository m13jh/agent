"""工具审批服务抽象及其常用实现。"""

from __future__ import annotations

import inspect
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
