"""阶段 1 的单次模型 → 工具 → 模型 Agent Loop。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from python_agent.approval.service import ApprovalService
from python_agent.config import AgentPreset
from python_agent.core.inbox import UserMessage
from python_agent.core.lifecycle import TaskStatus, task_status_for_finish_reason
from python_agent.core.limits import BudgetExceededError, BudgetViolation, TurnBudget
from python_agent.errors import ModelError
from python_agent.ids import new_message_id
from python_agent.llm.adapter import ModelAdapter, ModelRouter
from python_agent.llm.retry import (
    DefaultModelRetryPolicy,
    ModelRetryContext,
    ModelRetryPolicy,
    RetryDecision,
)
from python_agent.llm.types import (
    AssistantResponse,
    ModelChunk,
    ModelRequest,
    ToolCall,
    Usage,
    tool_call_data,
)
from python_agent.prompt.assembler import PromptAssembler
from python_agent.session.session import Session
from python_agent.tools.policies import ExecuteHandler, PostHandler, PreHandler
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext, ToolResult

EventHandler = Callable[[str, dict[str, Any]], None | Awaitable[None]]
StepInputProvider = Callable[[], list[UserMessage]]


@dataclass(slots=True)
class RunResult:
    """一次 Turn 的返回值。

    answer 是最后一次 assistant 文本，session 保存完整可回放事实，finish_reason 表示
    模型自然结束、达到上限、工具结束或其他终止原因。把三者一起返回，调用方可以只读
    答案，也可以继续检查事件和诊断信息。
    """

    answer: str
    session: Session
    finish_reason: str

    @property
    def session_id(self) -> str:
        """返回字符串形式的 Session ID，方便 CLI、日志和 JSON 序列化。"""

        return str(self.session.id)

    @property
    def task_status(self) -> TaskStatus:
        """把 Turn 终止原因转换成“最终完成”或“仍需继续”的任务状态。"""

        return task_status_for_finish_reason(self.finish_reason)


@dataclass(frozen=True, slots=True)
class ModelRequestStatus:
    """当前正在等待的模型请求快照，供 CLI `/status` 实时展示。"""

    turn: int
    step: int
    provider: str
    model: str
    attempt: int
    elapsed_seconds: float


class AgentLoop:
    """使用一个适配器和一个内存 Session 执行一次 prompt。

    阶段 1 暂不引入后台 Driver 或 Durable Inbox。所有资源都由当前协程直接持有，
    因此 ``run`` 返回前每一次模型请求和工具调用都会被明确 await，避免留下无人负责的
    asyncio Task。
    """

    def __init__(
        self,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        session: Session | None = None,
        system_prompt: str | None = None,
        workspace: Path | None = None,
        excluded_paths: tuple[Path, ...] = (),
        event_handler: EventHandler | None = None,
        approval_service: ApprovalService | None = None,
        approval_required: set[str] | frozenset[str] | None = None,
        spill_directory: Path | None = None,
        pre_policies: tuple[PreHandler, ...] = (),
        execute_policies: tuple[ExecuteHandler, ...] = (),
        post_policies: tuple[PostHandler, ...] = (),
        request_retry_policy: ModelRetryPolicy | None = None,
    ) -> None:
        """创建单次循环。

        这里组装工具策略，但不创建后台 Task；阶段 1的直接调用由当前协程负责资源，
        阶段 2的 Agent Handle 则把同一循环放进自己拥有的 Driver Task。
        """

        self.config = config or AgentPreset()
        self.adapter = adapter
        self.tools = tools or ToolRegistry()
        self.session = session or Session.new(
            agent_preset=self.config.id,
            cwd=workspace or self.config.workspace,
        )
        self.prompt_assembler = PromptAssembler.default()
        self.system_prompt = system_prompt or self.prompt_assembler.assemble()
        self.excluded_paths = tuple(path.expanduser().resolve() for path in excluded_paths)
        self.approval_service = approval_service
        self.approval_required = frozenset(
            self.config.approval_required if approval_required is None else approval_required
        )
        self.spill_directory = spill_directory
        self.pre_policies = tuple(pre_policies)
        self.execute_policies = tuple(execute_policies)
        self.post_policies = tuple(post_policies)
        self.cancel_event = asyncio.Event()
        self._runtime = ToolRuntime(
            self.tools,
            max_result_chars=self.config.max_tool_result_chars,
            max_parallel_tools=self.config.max_parallel_tools,
            spill_directory=spill_directory,
            approval_service=approval_service,
            approval_required=self.approval_required,
            pre_policies=pre_policies,
            execute_policies=execute_policies,
            post_policies=post_policies,
        )
        self.request_retry_policy = request_retry_policy or DefaultModelRetryPolicy(
            self.config.model_retry_base_delay_seconds
        )
        self.event_handler = event_handler
        # 只保存当前正在 await 的一次模型请求；单 Driver 规则保证同一 Agent 不会同时
        # 出现两个活动请求。开始时间使用 monotonic clock，不受系统时间校准影响。
        self._active_request: tuple[int, int, str, str, int, float] | None = None

    @property
    def active_request(self) -> ModelRequestStatus | None:
        """返回当前模型请求及已经等待的秒数；没有请求时返回 None。"""

        if self._active_request is None:
            return None
        turn, step, provider, model, attempt, started_at = self._active_request
        return ModelRequestStatus(
            turn=turn,
            step=step,
            provider=provider,
            model=model,
            attempt=attempt,
            elapsed_seconds=max(0.0, time.monotonic() - started_at),
        )

    def cancel(self) -> None:
        """请求协作式取消当前正在进行的模型或工具操作。

        取消信号不会强行修改事件日志；Agent Loop 会在安全边界记录 ``aborted``，并把
        ``CancelledError`` 交还给调用方处理。
        """

        self.cancel_event.set()

    def reset_cancel(self) -> None:
        """在上一个 Driver 已经收敛后清除取消信号，允许下一个 Turn 重新运行。"""

        self.cancel_event.clear()

    def _resolve_adapter(self) -> ModelAdapter:
        """解析当前请求要使用的适配器。

        直接传入 Adapter 时原样返回；传入 ModelRouter 时根据配置里的 provider 查找，
        使 Agent Loop 不需要保存或理解具体 Provider 的客户端。
        """

        if isinstance(self.adapter, ModelRouter):
            return self.adapter.resolve(self.config.provider)
        return self.adapter

    def _tool_schemas(self) -> list[dict[str, object]]:
        """生成发送给模型的工具 Schema，并按配置限制可见工具。

        空的 config.tools 表示暴露注册表中的全部工具；显式列表则先通过 Registry.get
        校验每个名字存在，避免模型看到一个实际无法执行的工具。
        """

        if not self.config.tools:
            return self.tools.schemas()
        selected = [self.tools.get(name) for name in self.config.tools]
        result: list[dict[str, object]] = []
        for tool in selected:
            schema = getattr(tool, "schema", None)
            result.append(
                schema()
                if callable(schema)
                else {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return result

    def _request(self) -> ModelRequest:
        """从当前 Session 投影消息，创建本次不可变的 ModelRequest。"""

        return ModelRequest(
            provider=self.config.provider,
            model=self.config.model,
            system=self.system_prompt,
            messages=self.session.messages(),
            tools=self._tool_schemas(),
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """把实时观察事件交给 CLI 或上层 UI，不改变 Session 的权威事实。"""

        if self.event_handler is not None:
            result = self.event_handler(event_type, data)
            if isinstance(result, Awaitable):
                await result

    async def _complete_response(
        self,
        adapter: ModelAdapter,
        request: ModelRequest,
        *,
        turn: int,
        step: int,
    ) -> tuple[AssistantResponse, bool]:
        """统一处理流式和非流式 Adapter，并返回完整的标准化响应。

        流式模式只负责把文本片段实时通知出去；Session 仍然在整个响应完成后记录一个
        完整的 ``assistant/message``。这样用户有即时反馈，同时事件日志不会保存半截的
        assistant 消息或半截的工具参数。
        """

        stream = getattr(adapter, "stream", None)
        if not callable(stream):
            raw_response = await adapter.complete(request, cancel_event=self.cancel_event)
            response = (
                raw_response
                if isinstance(raw_response, AssistantResponse)
                else AssistantResponse.model_validate(raw_response)
            )
            return self._safe_response(response), False

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        tool_call_positions: dict[str, int] = {}
        finish_reason: Literal["stop", "tool_calls", "length", "error"] | None = None
        usage = Usage()
        saw_done = False
        saw_finish_reason = False
        chunk_count = 0
        async for raw_chunk in stream(request, cancel_event=self.cancel_event):
            if saw_done:
                raise ModelError("LLM_MALFORMED_RESPONSE: stream emitted data after done")
            chunk = (
                raw_chunk
                if isinstance(raw_chunk, ModelChunk)
                else ModelChunk.model_validate(raw_chunk)
            )
            chunk_count += 1
            if chunk.content:
                content_parts.append(chunk.content)
                await self._emit(
                    "assistant/delta",
                    {"turn": turn, "step": step, "content": chunk.content},
                )
            if chunk.tool_calls:
                # 适配器可能在多个分片中发出完整调用。保留首次出现顺序，重复 call id 则使用
                # 最新的完整值替换。
                for call in chunk.tool_calls:
                    call_id = str(call.id)
                    position = tool_call_positions.get(call_id)
                    if position is None:
                        tool_call_positions[call_id] = len(tool_calls)
                        tool_calls.append(call)
                    else:
                        tool_calls[position] = call
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
                saw_finish_reason = True
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.done:
                saw_done = True
        if not saw_done:
            raise ModelError(
                "LLM_STREAM_CLOSED: model stream ended without a terminal chunk "
                f"after {chunk_count} chunks"
            )
        if not saw_finish_reason or finish_reason is None:
            raise ModelError("LLM_MALFORMED_RESPONSE: terminal stream chunk has no finish_reason")
        if finish_reason == "error":
            raise ModelError("LLM_PROVIDER_ERROR: provider returned finish_reason=error")
        # 达到长度上限的响应可能包含语法上看似完整的工具调用前缀；即使自定义适配器提供了
        # ToolCall 数据，也绝不能执行。
        if finish_reason == "length":
            tool_calls = []
        response = AssistantResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
        return response, True

    @staticmethod
    def _safe_response(response: AssistantResponse) -> AssistantResponse:
        """为只支持完整响应的适配器应用与流式适配器相同的不可执行保护。"""

        if response.finish_reason == "error":
            raise ModelError("LLM_PROVIDER_ERROR: provider returned finish_reason=error")
        if response.finish_reason == "length" and response.tool_calls:
            return response.model_copy(update={"tool_calls": []})
        return response

    async def _request_with_status(
        self,
        adapter: ModelAdapter,
        request: ModelRequest,
        *,
        turn: int,
        step: int,
        attempt: int = 1,
    ) -> tuple[AssistantResponse, bool]:
        """包装模型请求，发布开始/结束通知并维护可查询的实时状态。

        这些通知属于 UI/指标事实，不写进模型上下文；持久化的 ``request/header`` 仍由
        run_turn 在调用本方法前追加。无论请求完成、报错还是被取消，finally 都会清理
        active_request，避免 `/status` 在请求结束后继续显示过期状态。
        """

        started_at = time.monotonic()
        self._active_request = (
            turn,
            step,
            request.provider,
            request.model,
            attempt,
            started_at,
        )
        status = "completed"
        finish_reason: str | None = None
        error_type: str | None = None
        try:
            await self._emit(
                "model/request_start",
                {
                    "turn": turn,
                    "step": step,
                    "provider": request.provider,
                    "model": request.model,
                    "attempt": attempt,
                    "message_count": len(request.messages),
                    "tool_count": len(request.tools),
                },
            )
            response, streamed = await self._complete_response(
                adapter,
                request,
                turn=turn,
                step=step,
            )
            finish_reason = response.finish_reason
            return response, streamed
        except asyncio.CancelledError:
            status = "cancelled"
            error_type = "CancelledError"
            raise
        except Exception as exc:
            status = "error"
            error_type = type(exc).__name__
            raise
        finally:
            duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
            self._active_request = None
            await self._emit(
                "model/request_end",
                {
                    "turn": turn,
                    "step": step,
                    "provider": request.provider,
                    "model": request.model,
                    "attempt": attempt,
                    "status": status,
                    "finish_reason": finish_reason,
                    "error_type": error_type,
                    "duration_ms": duration_ms,
                },
            )

    async def _request_with_retries(
        self,
        adapter: ModelAdapter,
        request: ModelRequest,
        *,
        turn: int,
        step: int,
        budget: TurnBudget,
    ) -> tuple[AssistantResponse, bool]:
        """在 Turn 墙钟预算内执行模型请求，并让策略决定有限重试。

        每次失败都会持久化 ``request/error``；只有策略批准时再追加 ``request/retry``。
        重试不会复制 user/message 或打开新 Step，因此恢复后的模型历史仍然只包含一次
        语义请求。每个网络尝试都有独立的实时开始/结束与耗时事件。
        """

        attempt = 1
        while True:
            remaining = budget.remaining_wall_seconds
            if remaining is not None and remaining <= 0:
                raise BudgetExceededError(
                    BudgetViolation("wall_time", "模型请求开始前 Turn 墙钟预算已经耗尽")
                )
            try:
                operation = self._request_with_status(
                    adapter,
                    request,
                    turn=turn,
                    step=step,
                    attempt=attempt,
                )
                if remaining is None:
                    return await operation
                try:
                    return await asyncio.wait_for(operation, timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise BudgetExceededError(
                        BudgetViolation(
                            "wall_time",
                            f"模型请求超过 Turn 剩余墙钟预算 {remaining:.3f}s",
                        )
                    ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, BudgetExceededError):
                    decision = RetryDecision(False, reason="Turn 预算错误不允许重试")
                else:
                    context = ModelRetryContext(
                        request=request,
                        error=exc,
                        attempt=attempt,
                        max_retries=self.config.model_max_retries,
                        elapsed_seconds=budget.elapsed_seconds,
                    )
                    try:
                        decision = await self.request_retry_policy.decide(context)
                    except Exception as policy_error:
                        decision = RetryDecision(
                            False,
                            reason=f"重试策略异常：{type(policy_error).__name__}",
                        )

                error_data = {
                    "turn": turn,
                    "step": step,
                    "attempt": attempt,
                    "provider": request.provider,
                    "model": request.model,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "will_retry": decision.retry,
                    "delay_seconds": max(0.0, decision.delay_seconds),
                    "reason": decision.reason,
                }
                # 请求诊断不改变模型消息语义，标记 ignorable 让旧投影器也能安全跳过。
                self.session.append("request/error", error_data, ignorable=True)
                await self._emit("model/request_error", error_data)
                if not decision.retry:
                    raise

                delay = max(0.0, decision.delay_seconds)
                remaining = budget.remaining_wall_seconds
                if remaining is not None and delay >= remaining:
                    raise BudgetExceededError(
                        BudgetViolation(
                            "wall_time",
                            "模型重试退避时间将耗尽 Turn 剩余墙钟预算",
                        )
                    ) from exc
                retry_data = {
                    "turn": turn,
                    "step": step,
                    "next_attempt": attempt + 1,
                    "delay_seconds": delay,
                    "reason": decision.reason,
                }
                self.session.append("request/retry", retry_data, ignorable=True)
                await self._emit("model/request_retry", retry_data)
                if delay > 0:
                    try:
                        await asyncio.wait_for(self.cancel_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    else:
                        raise asyncio.CancelledError
                attempt += 1

    async def run(self, prompt: str) -> RunResult:
        """执行一个没有外部 Inbox 的独立 Turn，保持阶段 1 的兼容 API。"""

        return await self.run_turn(prompt)

    async def run_turn(
        self,
        prompt: str | UserMessage,
        *,
        step_input_provider: StepInputProvider | None = None,
    ) -> RunResult:
        """运行用户 prompt，直到模型给出最终回答或达到配置的步骤上限。

        每个步骤都会先写入 ``step/start`` 和 request/header，再调用模型；模型回答、工具
        调用、工具结果和 ``step/end`` 按实际发生顺序追加到 Session。只要模型产生工具
        调用，工具结果就会进入下一次 ModelRequest 的消息投影。
        """

        prompt_content = prompt.content if isinstance(prompt, UserMessage) else prompt
        if not isinstance(prompt_content, str) or not prompt_content.strip():
            raise ValueError("prompt must be a non-empty string")
        # Agent Handle 传入完整 UserMessage 时必须沿用 Inbox MessageId。这样进程重启后可以
        # 区分“claim 已正式写入模型历史”和“刚出队就崩溃”两种状态；阶段 1 直接传字符串
        # 时仍在这里生成新 ID，保持原有 API 兼容。
        prompt_message_id = (
            prompt.message_id if isinstance(prompt, UserMessage) else new_message_id()
        )
        prompt_kind = prompt.kind if isinstance(prompt, UserMessage) else "followup"

        budget = TurnBudget(self.config)
        turn = self.session.next_turn()
        self.session.append("turn/start", {"turn": turn})
        last_answer = ""
        adapter = self._resolve_adapter()
        prompt_pending = True
        pending_step_messages: list[UserMessage] = []

        def append_turn_end(reason: str, *, limit: BudgetViolation | None = None) -> None:
            """统一关闭 Turn，并把阶段五累计预算快照写入权威日志。"""

            data: dict[str, Any] = {
                "turn": turn,
                "reason": reason,
                "budget": budget.snapshot().event_data(),
            }
            if limit is not None:
                data["limit"] = {"reason": limit.reason, "message": limit.message}
            self.session.append("turn/end", data)

        async def finish_for_limit(
            violation: BudgetViolation,
            *,
            step: int | None = None,
        ) -> RunResult:
            """在可选 Step 边界记录限制，关闭 Turn 并返回正常的 RunResult。"""

            limit_data = {
                "turn": turn,
                "step": step,
                "reason": violation.reason,
                "message": violation.message,
                "task_status": "paused",
                "budget": budget.snapshot().event_data(),
            }
            if step is not None:
                self.session.append(
                    "step/end",
                    {
                        "turn": turn,
                        "step": step,
                        "reason": violation.reason,
                        "limit": violation.message,
                    },
                )
            await self._emit("agent/limit", limit_data)
            append_turn_end(violation.reason, limit=violation)
            return RunResult(last_answer, self.session, violation.reason)

        try:
            # 1. max_steps 提供硬循环上限，其余预算在每个 Step 边界动态检查。
            for _ in range(self.config.max_steps):
                if self.cancel_event.is_set():
                    raise asyncio.CancelledError
                boundary_violation = budget.check_boundary()
                if boundary_violation is not None:
                    return await finish_for_limit(boundary_violation)

                step = self.session.next_step()
                self.session.append("step/start", {"turn": turn, "step": step})
                step_closed = False
                try:
                    # 2. 第一次循环写主 prompt；后续 Step 只消费 steer/inject 和工具结果。
                    if prompt_pending:
                        self.session.append(
                            "user/message",
                            {
                                "message_id": str(prompt_message_id),
                                "content": prompt_content,
                                "input_kind": prompt_kind,
                            },
                        )
                        prompt_pending = False
                    step_inputs = pending_step_messages
                    pending_step_messages = []
                    if step_input_provider is not None:
                        step_inputs.extend(step_input_provider())
                    for message in step_inputs:
                        self.session.append(
                            "user/message",
                            {
                                "message_id": str(message.message_id),
                                "content": message.content,
                                "input_kind": message.kind,
                            },
                        )

                    # 3. request/header 记录稳定请求配置和发起前预算，重试本身不复制消息。
                    request = self._request()
                    remaining_tokens = budget.remaining_tokens
                    if remaining_tokens is not None:
                        # 无 Provider tokenizer 时无法预知下一请求的 prompt_tokens，但至少把
                        # completion 上限压到剩余总预算以内，避免一次输出造成无界超支。
                        request = request.model_copy(
                            update={"max_tokens": min(request.max_tokens, remaining_tokens)}
                        )
                    self.session.append(
                        "request/header",
                        {
                            "turn": turn,
                            "step": step,
                            "provider": request.provider,
                            "model": request.model,
                            "system": request.system,
                            "tools": request.tools,
                            "max_tokens": request.max_tokens,
                            "temperature": request.temperature,
                            "max_retries": self.config.model_max_retries,
                            "budget_before": budget.snapshot().event_data(),
                        },
                    )
                    try:
                        response, streamed = await self._request_with_retries(
                            adapter,
                            request,
                            turn=turn,
                            step=step,
                            budget=budget,
                        )
                    except BudgetExceededError as exc:
                        step_closed = True
                        return await finish_for_limit(exc.violation, step=step)

                    response = self._safe_response(response)
                    last_answer = response.content or ""
                    assistant_event_data = {
                        "content": response.content,
                        "tool_calls": [tool_call_data(call) for call in response.tool_calls],
                        "finish_reason": response.finish_reason,
                        "usage": response.usage.model_dump(),
                        "streamed": streamed,
                    }
                    self.session.append("assistant/message", assistant_event_data)
                    await self._emit("assistant/message", assistant_event_data)
                    usage_violation = budget.record_usage(response.usage)

                    if not response.tool_calls:
                        if usage_violation is not None:
                            step_closed = True
                            return await finish_for_limit(usage_violation, step=step)
                        # 模型自然结束前再检查 next_step，捕获模型请求期间到达的 steer。
                        if step_input_provider is not None:
                            pending_step_messages = step_input_provider()
                        if pending_step_messages:
                            self.session.append(
                                "step/end",
                                {"turn": turn, "step": step, "reason": "next_step_input"},
                            )
                            step_closed = True
                            continue
                        reason = "max_tokens" if response.finish_reason == "length" else "completed"
                        self.session.append(
                            "step/end",
                            {"turn": turn, "step": step, "reason": reason},
                        )
                        step_closed = True
                        append_turn_end(reason)
                        return RunResult(last_answer, self.session, response.finish_reason)

                    # 4. 先按模型顺序记录全部 call，再允许 Runtime 并发执行，确保审计完整。
                    for call in response.tool_calls:
                        tool_call_event_data = {
                            "turn": turn,
                            "step": step,
                            "call_id": str(call.id),
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        self.session.append("tool/call", tool_call_event_data)
                        await self._emit("tool/call", tool_call_event_data)
                        if call.name in {"write_file", "apply_patch"}:
                            self.session.append("tool/write_intent", tool_call_event_data)
                            await self._emit("tool/write_intent", tool_call_event_data)

                    tool_context = ToolContext(
                        session_id=self.session.id,
                        workspace=self.session.header.cwd,
                        cancel_event=self.cancel_event,
                        permission_mode=self.config.permission_mode,
                        excluded_paths=self.excluded_paths,
                    )
                    tool_violation = usage_violation
                    if tool_violation is not None:
                        tool_results = [
                            ToolResult(
                                call_id=call.id,
                                name=call.name,
                                content=f"工具未执行：{tool_violation.message}",
                                is_error=True,
                            )
                            for call in response.tool_calls
                        ]
                    else:
                        remaining = budget.remaining_wall_seconds
                        if remaining is not None and remaining <= 0:
                            tool_violation = BudgetViolation(
                                "wall_time",
                                "工具开始前 Turn 墙钟预算已经耗尽",
                            )
                            tool_results = [
                                ToolResult(
                                    call_id=call.id,
                                    name=call.name,
                                    content=f"工具未执行：{tool_violation.message}",
                                    is_error=True,
                                )
                                for call in response.tool_calls
                            ]
                        else:
                            operation = self._runtime.execute_many(
                                response.tool_calls,
                                tool_context,
                            )
                            try:
                                tool_results = (
                                    await operation
                                    if remaining is None
                                    else await asyncio.wait_for(operation, timeout=remaining)
                                )
                            except asyncio.TimeoutError:
                                tool_violation = BudgetViolation(
                                    "wall_time",
                                    "工具组执行超过 Turn 剩余墙钟预算",
                                )
                                tool_results = [
                                    ToolResult(
                                        call_id=call.id,
                                        name=call.name,
                                        content=f"工具执行被预算取消：{tool_violation.message}",
                                        is_error=True,
                                    )
                                    for call in response.tool_calls
                                ]

                    # 5. Runtime 返回值已恢复模型顺序；事件提交顺序不受实际完成先后影响。
                    for result in tool_results:
                        tool_result_event_data = result.event_data()
                        self.session.append("tool/result", tool_result_event_data)
                        await self._emit("tool/result", tool_result_event_data)

                    if tool_violation is None:
                        tool_violation = budget.check_boundary()
                    if tool_violation is not None:
                        step_closed = True
                        return await finish_for_limit(tool_violation, step=step)
                    if self.cancel_event.is_set():
                        raise asyncio.CancelledError

                    concluding_result = next(
                        (result for result in tool_results if result.concludes_turn),
                        None,
                    )
                    if concluding_result is not None:
                        if step_input_provider is not None:
                            pending_step_messages = step_input_provider()
                        if pending_step_messages:
                            self.session.append(
                                "step/end",
                                {"turn": turn, "step": step, "reason": "next_step_input"},
                            )
                            step_closed = True
                            continue
                        self.session.append(
                            "step/end",
                            {"turn": turn, "step": step, "reason": "tool_concluded"},
                        )
                        step_closed = True
                        append_turn_end("tool_concluded")
                        return RunResult(
                            last_answer or str(concluding_result.content),
                            self.session,
                            "tool_concluded",
                        )

                    self.session.append(
                        "step/end",
                        {"turn": turn, "step": step, "reason": "tool_calls"},
                    )
                    step_closed = True
                except asyncio.CancelledError:
                    if not step_closed:
                        self.session.append(
                            "step/end",
                            {"turn": turn, "step": step, "reason": "aborted"},
                        )
                    raise
                except Exception:
                    if not step_closed:
                        self.session.append(
                            "step/end",
                            {"turn": turn, "step": step, "reason": "error"},
                        )
                    raise

            max_steps = BudgetViolation(
                "max_steps",
                f"Turn 已达到 max_steps={self.config.max_steps}，尚未生成最终回答；"
                "请使用 /continue 继续，或提交新任务。",
            )
            return await finish_for_limit(max_steps)
        except asyncio.CancelledError:
            append_turn_end("aborted")
            raise
        except Exception:
            append_turn_end("error")
            raise
