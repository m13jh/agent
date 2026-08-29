"""工具统一运行时：Pre → Execute → Post 策略、超时和结果规范化。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from python_agent.approval.service import ApprovalService
from python_agent.errors import ToolError
from python_agent.hooks.waterfall import Waterfall
from python_agent.llm.types import ToolCall
from python_agent.tools.policies import (
    ExecuteHandler,
    OutputPolicy,
    PostHandler,
    PreHandler,
    TimeoutPolicy,
    ToolInvocation,
    ToolResultEnvelope,
    build_pre_waterfall,
    validate_arguments,
)
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.types import ToolContext, ToolResult


class ToolRuntime:
    """统一执行模型工具调用，并保持安全策略位于业务工具主体之外。

    Pre 策略负责参数、路径、权限和审批；Execute 策略负责超时等运行约束；Post 策略
    负责输出裁剪和 spill。无论是内置策略拒绝，还是工具主体抛出普通异常，最终都会
    形成模型可见的 ToolResult；只有协作式取消继续交给上层生命周期处理。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_result_chars: int = 12000,
        spill_directory: Path | None = None,
        approval_service: ApprovalService | None = None,
        approval_required: Iterable[str] | None = None,
        pre_policies: Iterable[PreHandler] = (),
        execute_policies: Iterable[ExecuteHandler] = (),
        post_policies: Iterable[PostHandler] = (),
    ) -> None:
        """组装三段策略链。

        内置安全策略始终先于调用方自定义策略；调用方可以在其后增加审计、指标或
        业务限制，而不能绕过参数、路径、权限和审批检查。
        """

        self.registry = registry
        self.max_result_chars = max_result_chars
        self.approval_service = approval_service
        required = frozenset({"bash"} if approval_required is None else approval_required)
        self.pre_waterfall = build_pre_waterfall(
            approval_service,
            required,
            tuple(pre_policies),
        )
        self.execute_waterfall = Waterfall([TimeoutPolicy(), *tuple(execute_policies)])
        self.post_waterfall = Waterfall(
            [OutputPolicy(max_result_chars, spill_directory), *tuple(post_policies)]
        )

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        """执行一个调用，并把所有非取消异常转成标准错误结果。"""

        tool = self.registry.maybe_get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"tool is not registered: {call.name}",
                is_error=True,
            )

        if self.approval_service is not None and context.approval_service is None:
            context = replace(context, approval_service=self.approval_service)
        invocation = ToolInvocation(call=call, tool=tool, context=context)
        try:
            pre_result = await self.pre_waterfall.run(invocation, self._pre_terminal)
            if isinstance(pre_result, ToolResult):
                result = pre_result
            else:
                raw_result = await self.execute_waterfall.run(invocation, self._execute_terminal)
                if isinstance(raw_result, ToolResult):
                    result = raw_result.model_copy(update={"call_id": call.id, "name": call.name})
                else:
                    result = ToolResult(
                        call_id=call.id,
                        name=call.name,
                        content=raw_result,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = self._error_result(call, exc)

        try:
            post_value = ToolResultEnvelope(invocation=invocation, result=result)
            return cast(
                ToolResult,
                await self.post_waterfall.run(post_value, self._post_terminal),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._error_result(call, exc)

    @staticmethod
    async def _pre_terminal(invocation: ToolInvocation) -> ToolResult | None:
        """Pre 链的放行终点；None 表示允许进入 Execute 链。"""

        del invocation
        return None

    async def _execute_terminal(self, invocation: ToolInvocation) -> Any:
        """调用业务工具主体，不在业务工具里混入超时和权限逻辑。"""

        return await invocation.tool.execute(
            invocation.call.arguments,
            invocation.context,
        )

    @staticmethod
    async def _post_terminal(envelope: ToolResultEnvelope) -> ToolResult:
        """Post 链的默认终点。"""

        return envelope.result

    @staticmethod
    def _error_result(call: ToolCall, exc: Exception) -> ToolResult:
        """统一格式化异常，避免工具异常破坏 Agent 主循环。"""

        if isinstance(exc, ToolError):
            message = str(exc)
        else:
            message = f"{type(exc).__name__}: {exc}"
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=message,
            is_error=True,
        )


__all__ = ["ToolContext", "ToolResult", "ToolRuntime", "validate_arguments"]
