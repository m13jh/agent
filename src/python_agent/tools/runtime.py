"""工具统一运行时：Pre → Execute → Post 策略、超时和结果规范化。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from python_agent.approval.service import ApprovalService
from python_agent.errors import ToolError
from python_agent.hooks.waterfall import Waterfall
from python_agent.llm.types import ToolCall
from python_agent.tools.delete_policy import DeletePolicyEngine
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
        max_parallel_tools: int = 4,
        spill_directory: Path | None = None,
        approval_service: ApprovalService | None = None,
        approval_required: Iterable[str] | None = None,
        delete_policy_engine: DeletePolicyEngine | None = None,
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
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.approval_service = approval_service
        self.delete_policy_engine = delete_policy_engine
        required = frozenset({"bash"} if approval_required is None else approval_required)
        self.pre_waterfall = build_pre_waterfall(
            approval_service,
            required,
            tuple(pre_policies),
            delete_policy_engine,
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
        if self.delete_policy_engine is not None and context.delete_policy is None:
            context = replace(context, delete_policy=self.delete_policy_engine)
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

    async def execute_many(
        self,
        calls: Sequence[ToolCall],
        context: ToolContext,
    ) -> list[ToolResult]:
        """按模型顺序调度一批工具，并以相同顺序返回结果。

        连续的并发安全调用组成 parallel batch，由 Semaphore 构成有界滚动池；exclusive
        调用在执行前先排空前一批，完成后才允许启动后一批，因此天然形成读/写屏障。
        实际完成顺序不会影响返回列表位置，AgentLoop 可据此按模型 call 顺序提交事件。
        """

        if not calls:
            return []
        results: list[ToolResult | None] = [None] * len(calls)
        parallel_batch: list[tuple[int, ToolCall]] = []

        async def flush_parallel_batch() -> None:
            """执行当前只读批次并把结果写回其模型序号位置。"""

            nonlocal parallel_batch
            if not parallel_batch:
                return
            batch_results = await self._execute_parallel_batch(parallel_batch, context)
            for (index, _), result in zip(parallel_batch, batch_results, strict=True):
                results[index] = result
            parallel_batch = []

        for index, call in enumerate(calls):
            if self._is_concurrency_safe(call):
                parallel_batch.append((index, call))
                continue

            # exclusive 工具是屏障：它不能和前后的并发安全工具发生重叠。
            await flush_parallel_batch()
            if context.cancel_event.is_set():
                results[index] = self._cancelled_result(call, "工具尚未启动，Agent 已取消")
                continue
            try:
                results[index] = await self.execute(call, context)
            except asyncio.CancelledError:
                # Driver Task 的取消在此转换为明确结果；AgentLoop 写完所有 call/result 后
                # 会根据同一个 cancel_event 再抛 CancelledError，保持生命周期语义。
                if not context.cancel_event.is_set():
                    # 例如外层 wall-time 的 wait_for 取消：不是用户取消，必须继续向上传播，
                    # 让预算层把它转换成 wall_time，而不能污染 Agent 的长期 cancel_event。
                    raise
                results[index] = self._cancelled_result(call, "exclusive 工具执行期间被取消")

        await flush_parallel_batch()
        finalized: list[ToolResult] = []
        for index, result in enumerate(results):
            if result is None:  # pragma: no cover - 防御调度器内部遗漏
                result = self._cancelled_result(calls[index], "工具调度未产生结果")
            finalized.append(result)
        return finalized

    def _is_concurrency_safe(self, call: ToolCall) -> bool:
        """查询工具对本次参数的并发声明；缺失或声明异常时保守视为 exclusive。"""

        tool = self.registry.maybe_get(call.name)
        if tool is None:
            return False
        try:
            return bool(tool.is_concurrency_safe(call.arguments))
        except Exception:
            return False

    async def _execute_parallel_batch(
        self,
        indexed_calls: Sequence[tuple[int, ToolCall]],
        context: ToolContext,
    ) -> list[ToolResult]:
        """用有界 Semaphore 执行一个 parallel batch，并确保所有 Task 被当前调用拥有。"""

        semaphore = asyncio.Semaphore(self.max_parallel_tools)

        async def worker(call: ToolCall) -> ToolResult:
            """只有取得槽位且未取消时才进入工具流水线。"""

            if context.cancel_event.is_set():
                return self._cancelled_result(call, "工具尚未启动，Agent 已取消")
            async with semaphore:
                if context.cancel_event.is_set():
                    return self._cancelled_result(call, "工具等待并发槽位期间被取消")
                try:
                    return await self.execute(call, context)
                except asyncio.CancelledError:
                    if not context.cancel_event.is_set():
                        raise
                    return self._cancelled_result(call, "并发工具执行期间被取消")

        tasks = [
            asyncio.create_task(worker(call), name=f"tool-{call.name}-{call.id}")
            for _, call in indexed_calls
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            # gather 被 Driver 取消时，显式拥有并排空全部子 Task，绝不遗留后台工具。
            cooperative_cancel = context.cancel_event.is_set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            settled = await asyncio.gather(*tasks, return_exceptions=True)
            if not cooperative_cancel:
                raise
            return [
                value
                if isinstance(value, ToolResult)
                else self._cancelled_result(call, "并发工具组收敛期间被取消")
                for (_, call), value in zip(indexed_calls, settled, strict=True)
            ]

    @staticmethod
    def _cancelled_result(call: ToolCall, message: str) -> ToolResult:
        """为未启动或被取消的调用生成可持久化错误结果。"""

        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=message,
            is_error=True,
        )

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


PolicyGateway = ToolRuntime


__all__ = ["PolicyGateway", "ToolContext", "ToolResult", "ToolRuntime", "validate_arguments"]
