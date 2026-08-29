"""定义模型适配器协议，并提供按 Provider 路由适配器的实现。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Protocol, runtime_checkable

from python_agent.errors import ModelError
from python_agent.llm.types import AssistantResponse, ModelChunk, ModelRequest


@runtime_checkable
class ModelAdapter(Protocol):
    """非流式模型适配器协议。

    Provider 的 HTTP、SSE 和错误格式都应在适配器内部转换；AgentLoop 只依赖统一的
    ModelRequest、AssistantResponse 和 cancel_event。
    """

    name: str

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """返回一次完整的标准化模型响应。"""

        ...


@runtime_checkable
class StreamingModelAdapter(Protocol):
    """可选的流式模型适配器协议，不强制旧的 complete-only 适配器实现。

    AgentLoop 通过能力检测决定调用 stream 还是 complete，因此老适配器仍然可以接入。
    """

    async def stream(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[ModelChunk]:
        """按到达顺序返回文本、工具调用和结束状态的增量片段。"""

        ...


class ModelRouter:
    """按 Provider 名称解析适配器，让 Agent Loop 不依赖具体 HTTP 客户端。"""

    def __init__(self, adapters: Mapping[str, ModelAdapter] | None = None) -> None:
        """复制 Provider 到适配器的映射，防止外部修改原 Mapping 影响路由。"""

        self._adapters: dict[str, ModelAdapter] = dict(adapters or {})

    def register(self, adapter: ModelAdapter) -> None:
        """注册适配器；重复 Provider 直接报错，避免静默替换已有实现。"""

        if adapter.name in self._adapters:
            raise ModelError(f"model provider already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def resolve(self, provider: str) -> ModelAdapter:
        """按 Provider 名称返回适配器，不存在时抛出 ModelError。"""

        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise ModelError(f"no adapter registered for provider: {provider}") from exc
