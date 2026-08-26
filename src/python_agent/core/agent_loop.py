"""阶段 1 的单次模型 → 工具 → 模型 Agent Loop。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from python_agent.config import AgentPreset
from python_agent.ids import new_message_id
from python_agent.llm.adapter import ModelAdapter, ModelRouter
from python_agent.llm.types import ModelRequest, tool_call_data
from python_agent.prompt.assembler import PromptAssembler
from python_agent.session.session import Session
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext


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

    def cancel(self) -> None:
        """请求协作式取消当前正在进行的模型或工具操作。

        取消信号不会强行修改事件日志；Agent Loop 会在安全边界记录 ``aborted``，并把
        ``CancelledError`` 交还给调用方处理。
        """

        self.cancel_event.set()

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

    async def run(self, prompt: str) -> RunResult:
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

        try:
            for _ in range(self.config.max_steps):
                if self.cancel_event.is_set():
                    raise asyncio.CancelledError
                step = self.session.next_step()
                self.session.append("step/start", {"turn": turn, "step": step})
                step_closed = False
                try:
                    if prompt_pending:
                        self.session.append(
                            "user/message",
                            {"message_id": str(new_message_id()), "content": prompt},
                        )
                        prompt_pending = False
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
                    response = await adapter.complete(request, cancel_event=self.cancel_event)
                    last_answer = response.content or ""
                    self.session.append(
                        "assistant/message",
                        {
                            "content": response.content,
                            "tool_calls": [tool_call_data(call) for call in response.tool_calls],
                            "finish_reason": response.finish_reason,
                            "usage": response.usage.model_dump(),
                        },
                    )

                    if not response.tool_calls:
                        reason = "max_tokens" if response.finish_reason == "length" else "completed"
                        self.session.append(
                            "step/end",
                            {"turn": turn, "step": step, "reason": reason},
                        )
                        step_closed = True
                        self.session.append("turn/end", {"turn": turn, "reason": reason})
                        return RunResult(last_answer, self.session, response.finish_reason)

                    for call in response.tool_calls:
                        self.session.append(
                            "tool/call",
                            {
                                "turn": turn,
                                "step": step,
                                "call_id": str(call.id),
                                "name": call.name,
                                "arguments": call.arguments,
                            },
                        )
                        result = await self._runtime.execute(
                            call,
                            ToolContext(
                                session_id=self.session.id,
                                workspace=self.session.header.cwd,
                                cancel_event=self.cancel_event,
                            ),
                        )
                        self.session.append("tool/result", result.event_data())
                        if result.concludes_turn:
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
