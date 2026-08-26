"""用于测试和离线 CLI 的确定性 Fake Adapter。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable
from typing import Any

from python_agent.llm.types import AssistantResponse, ModelRequest

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
