"""阶段 1 的单次模型 → 工具 → 模型 Agent Loop。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from python_agent.config import AgentPreset
from python_agent.core.inbox import UserMessage
from python_agent.ids import new_message_id
from python_agent.llm.adapter import ModelAdapter, ModelRouter
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
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext

EventHandler = Callable[[str, dict[str, Any]], None | Awaitable[None]]
StepInputProvider = Callable[[], list[UserMessage]]


@dataclass(slots=True)
class RunResult:
    answer: str
    session: Session
    finish_reason: str

    @property
    def session_id(self) -> str:
        return str(self.session.id)


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
        event_handler: EventHandler | None = None,
    ) -> None:
        self.config = config or AgentPreset()
        self.adapter = adapter
        self.tools = tools or ToolRegistry()
        self.session = session or Session.new(
            agent_preset=self.config.id,
            cwd=workspace or self.config.workspace,
        )
        self.prompt_assembler = PromptAssembler.default()
        self.system_prompt = system_prompt or self.prompt_assembler.assemble()
        self.cancel_event = asyncio.Event()
        self._runtime = ToolRuntime(self.tools, max_result_chars=self.config.max_tool_result_chars)
        self.event_handler = event_handler

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
        if isinstance(self.adapter, ModelRouter):
            return self.adapter.resolve(self.config.provider)
        return self.adapter

    def _tool_schemas(self) -> list[dict[str, object]]:
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
            response = await adapter.complete(request, cancel_event=self.cancel_event)
            return response, False

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
        usage = Usage()
        async for raw_chunk in stream(request, cancel_event=self.cancel_event):
            chunk = (
                raw_chunk
                if isinstance(raw_chunk, ModelChunk)
                else ModelChunk.model_validate(raw_chunk)
            )
            if chunk.content:
                content_parts.append(chunk.content)
                await self._emit(
                    "assistant/delta",
                    {"turn": turn, "step": step, "content": chunk.content},
                )
            if chunk.tool_calls:
                tool_calls = chunk.tool_calls
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            if chunk.usage is not None:
                usage = chunk.usage
        response = AssistantResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
        return response, True

    async def run(self, prompt: str) -> RunResult:
        """执行一个没有外部 Inbox 的独立 Turn，保持阶段 1 的兼容 API。"""

        return await self.run_turn(prompt)

    async def run_turn(
        self,
        prompt: str,
        *,
        step_input_provider: StepInputProvider | None = None,
    ) -> RunResult:
        """运行用户 prompt，直到模型给出最终回答或达到配置的步骤上限。

        每个步骤都会先写入 ``step/start`` 和 request/header，再调用模型；模型回答、工具
        调用、工具结果和 ``step/end`` 按实际发生顺序追加到 Session。只要模型产生工具
        调用，工具结果就会进入下一次 ModelRequest 的消息投影。
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        turn = self.session.next_turn()
        self.session.append("turn/start", {"turn": turn})
        last_answer = ""
        adapter = self._resolve_adapter()
        prompt_pending = True
        pending_step_messages: list[UserMessage] = []

        try:
            # 1. 通过经典 for 循环限制最大步骤数，防止模型和工具陷入死循环。
            for _ in range(self.config.max_steps):
                # 2. 每次发起新的 Step 前检查协作式取消信号。
                if self.cancel_event.is_set():
                    raise asyncio.CancelledError
                step = self.session.next_step()
                self.session.append("step/start", {"turn": turn, "step": step})
                step_closed = False
                try:
                    # 3. 第一次循环时把用户 prompt 写入 Session；后续循环只追加工具结果。
                    if prompt_pending:
                        self.session.append(
                            "user/message",
                            {"message_id": str(new_message_id()), "content": prompt},
                        )
                        prompt_pending = False
                    # 4. 从 Session 事件投影消息，并组装本次请求和工具 Schema。
                    #    Driver 提供的 steer/inject 会在这里进入下一个模型请求。
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
                    request = self._request()
                    self.session.append(
                        "request/header",
                        {
                            "turn": turn,
                            "step": step,
                            "provider": request.provider,
                            "model": request.model,
                            "system": request.system,
                            "tools": request.tools,
                        },
                    )
                    # 5. 发起模型请求：优先使用流式 stream()，否则回退到 complete()。
                    response, streamed = await self._complete_response(
                        adapter,
                        request,
                        turn=turn,
                        step=step,
                    )
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

                    if not response.tool_calls:
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
                        self.session.append("turn/end", {"turn": turn, "reason": reason})
                        return RunResult(last_answer, self.session, response.finish_reason)

                    # 6. 模型要求工具时，先记录并通知调用方，再交给 ToolRuntime 串行执行。
                    concluded_with_pending_input = False
                    for call in response.tool_calls:
                        tool_call_event_data = {
                            "turn": turn,
                            "step": step,
                            "call_id": str(call.id),
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        self.session.append(
                            "tool/call",
                            tool_call_event_data,
                        )
                        await self._emit("tool/call", tool_call_event_data)
                        result = await self._runtime.execute(
                            call,
                            ToolContext(
                                session_id=self.session.id,
                                workspace=self.session.header.cwd,
                                cancel_event=self.cancel_event,
                            ),
                        )
                        tool_result_event_data = result.event_data()
                        self.session.append("tool/result", tool_result_event_data)
                        await self._emit("tool/result", tool_result_event_data)
                        if result.concludes_turn:
                            if step_input_provider is not None:
                                pending_step_messages = step_input_provider()
                            if pending_step_messages:
                                self.session.append(
                                    "step/end",
                                    {
                                        "turn": turn,
                                        "step": step,
                                        "reason": "next_step_input",
                                    },
                                )
                                step_closed = True
                                concluded_with_pending_input = True
                                break
                            self.session.append(
                                "step/end",
                                {"turn": turn, "step": step, "reason": "tool_concluded"},
                            )
                            step_closed = True
                            self.session.append(
                                "turn/end",
                                {"turn": turn, "reason": "tool_concluded"},
                            )
                            return RunResult(
                                last_answer or str(result.content),
                                self.session,
                                "tool_concluded",
                            )

                    if concluded_with_pending_input:
                        continue
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

            self.session.append("turn/end", {"turn": turn, "reason": "max_steps"})
            return RunResult(last_answer, self.session, "max_steps")
        except asyncio.CancelledError:
            self.session.append("turn/end", {"turn": turn, "reason": "aborted"})
            raise
        except Exception:
            self.session.append("turn/end", {"turn": turn, "reason": "error"})
            raise
