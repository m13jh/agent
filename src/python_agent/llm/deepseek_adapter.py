"""轻量级的 OpenAI-compatible DeepSeek 适配器。

Provider 专属的请求和响应 JSON 转换被限制在本模块内。实现只使用 Python 标准库，
因此导入 Agent 核心不会强制安装额外的 HTTP 客户端；真实请求仍然通过异步线程执行，
不会阻塞 Agent 的事件循环。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from python_agent.errors import ModelError
from python_agent.ids import CallId
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall, Usage

load_dotenv("../../../.env")

class DeepSeekAdapter:
    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise ModelError("DEEPSEEK_API_KEY is required for the deepseek provider")

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = request.tools
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = await asyncio.to_thread(self._request, body)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        return self._parse_response(result)

    def _request(self, body: bytes) -> Mapping[str, object]:
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ModelError(f"DeepSeek request failed: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ModelError("DeepSeek response must be a JSON object")
        return value

    @staticmethod
    def _parse_response(value: Mapping[str, object]) -> AssistantResponse:
        """校验并转换 DeepSeek 返回的动态 JSON。

        Provider 响应属于不可信的外部边界，不能直接用下标链式访问。这里逐层检查
        choices、message、tool_calls、function 和 usage 的结构，任何不符合约定的内容
        都转成 ModelError，避免把底层 TypeError 泄漏给 Agent Loop。
        """

        def as_mapping(raw: object, label: str) -> Mapping[str, Any]:
            if not isinstance(raw, Mapping):
                raise ModelError(f"invalid DeepSeek response: {label} must be an object")
            return raw

        try:
            raw_choices = value.get("choices")
            if not isinstance(raw_choices, list) or not raw_choices:
                raise ModelError("invalid DeepSeek response: choices must be a non-empty list")
            choice = as_mapping(raw_choices[0], "choices[0]")
            message = as_mapping(choice.get("message"), "choices[0].message")
            content = message.get("content")
            raw_calls = message.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise ModelError("invalid DeepSeek response: tool_calls must be a list")
            calls: list[ToolCall] = []
            for index, raw_call in enumerate(raw_calls):
                call = as_mapping(raw_call, f"tool_calls[{index}]")
                function = as_mapping(call.get("function"), f"tool_calls[{index}].function")
                raw_arguments = function.get("arguments", "{}")
                if not isinstance(raw_arguments, str):
                    raise ModelError(
                        "invalid DeepSeek response: "
                        f"tool_calls[{index}].function.arguments must be text"
                    )
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ModelError(
                        "invalid DeepSeek response: "
                        f"tool_calls[{index}] arguments must be an object"
                    )
                raw_id = call.get("id")
                raw_name = function.get("name")
                if not isinstance(raw_id, str) or not isinstance(raw_name, str):
                    raise ModelError(
                        f"invalid DeepSeek response: tool_calls[{index}] lacks id or function.name"
                    )
                calls.append(
                    ToolCall(
                        id=CallId(raw_id),
                        name=raw_name,
                        arguments=arguments,
                    )
                )
            raw_usage = value.get("usage") or {}
            usage_data = as_mapping(raw_usage, "usage")
            usage = Usage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )
            reason = choice.get("finish_reason", "stop")
            if reason not in {"stop", "tool_calls", "length", "error"}:
                reason = "stop"
            return AssistantResponse(
                content=content,
                tool_calls=calls,
                finish_reason=reason,
                usage=usage,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError(f"invalid DeepSeek response: {exc}") from exc
