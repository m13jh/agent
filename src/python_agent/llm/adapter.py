"""定义模型适配器协议，并提供按 Provider 路由适配器的实现。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Protocol, runtime_checkable

from python_agent.errors import ModelError
from python_agent.llm.types import AssistantResponse, ModelChunk, ModelRequest


@runtime_checkable
class ModelAdapter(Protocol):
    name: str

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse: ...


@runtime_checkable
class StreamingModelAdapter(Protocol):
    """可选的流式模型适配器协议，不强制旧的 complete-only 适配器实现。"""

    async def stream(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[ModelChunk]: ...


class ModelRouter:
    """按 Provider 名称解析适配器，让 Agent Loop 不依赖具体 HTTP 客户端。"""

    def __init__(self, adapters: Mapping[str, ModelAdapter] | None = None) -> None:
        self._adapters: dict[str, ModelAdapter] = dict(adapters or {})

    def register(self, adapter: ModelAdapter) -> None:
        if adapter.name in self._adapters:
            raise ModelError(f"model provider already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def resolve(self, provider: str) -> ModelAdapter:
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise ModelError(f"no adapter registered for provider: {provider}") from exc
