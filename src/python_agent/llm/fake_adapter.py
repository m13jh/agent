"""用于测试和离线 CLI 的确定性 Fake Adapter。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from python_agent.llm.types import AssistantResponse, ModelChunk, ModelRequest

ResponseFactory = Callable[[ModelRequest], AssistantResponse | dict[str, Any]]


class FakeAdapter:
    """一个可控、可脚本化的模型适配器。

    ``responses`` 会按照加入顺序逐个返回；如果没有提供脚本，则返回确定性的 Echo 响应，
    这样 CLI 不需要 API Key 也能运行。调用方也可以传入 callable，根据每次 ModelRequest
    动态生成响应，从而覆盖“模型先调用工具、再根据工具结果回答”的集成测试场景。
    """

    name = "fake"

    def __init__(
        self,
        responses: Iterable[AssistantResponse | dict[str, Any]] | ResponseFactory | None = None,
    ) -> None:
        """保存脚本或响应工厂，并记录后续收到的 ModelRequest。"""

        self.requests: list[ModelRequest] = []
        self._factory = responses if callable(responses) else None
        self._responses = (
            list(responses) if responses is not None and not callable(responses) else []
        )

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """记录请求并返回脚本响应；没有脚本时根据最后一条 user 消息生成 Echo。"""

        if cancel_event.is_set():
            raise asyncio.CancelledError
        self.requests.append(request)
        if self._factory is not None:
            response = self._factory(request)
            if inspect.isawaitable(response):
                response = await response
            return AssistantResponse.model_validate(response)
        if self._responses:
            return AssistantResponse.model_validate(self._responses.pop(0))
        last_user = next(
            (
                message.get("content", "")
                for message in reversed(request.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        return AssistantResponse(content=f"Echo: {last_user}", finish_reason="stop")

    async def stream(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[ModelChunk]:
        """把完整 Fake 响应切成小片段，模拟真实模型的增量输出。

        Fake Adapter 的脚本仍然按照一次请求返回一个 AssistantResponse；这里只负责把
        文本拆开，方便测试和 CLI 验证流式显示。工具调用不会被伪造为半截 JSON，而是
        在最后一个 done 片段中一次性交给 Agent Loop，避免执行不完整的参数。
        """

        response = await self.complete(request, cancel_event=cancel_event)
        if response.content:
            for character in response.content:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                yield ModelChunk(content=character)
        yield ModelChunk(
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
            usage=response.usage,
            done=True,
        )
