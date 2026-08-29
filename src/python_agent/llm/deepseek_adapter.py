"""轻量级的 OpenAI-compatible DeepSeek 适配器。

Provider 专属的请求和响应 JSON 转换被限制在本模块内。普通请求保留标准库实现，
流式请求使用 httpx 的异步 SSE 接口；这样文本 delta 到达后可以立刻传给终端，而不必
等整个回答结束。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
from dotenv import find_dotenv, load_dotenv

from python_agent.errors import ModelError
from python_agent.ids import CallId
from python_agent.llm.types import AssistantResponse, ModelChunk, ModelRequest, ToolCall, Usage

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT_SECONDS = 120.0


def _load_environment(env_file: Path | None) -> Path | None:
    """加载项目配置文件，并返回实际使用的文件路径。

    相对路径不能直接写成 ``load_dotenv("../../../.env")``：这种路径是相对于进程的
    当前工作目录解析的，而不是相对于本 Python 文件解析的。这里优先使用调用命令所在
    目录的 ``.env``，再尝试源码项目根目录，最后使用 python-dotenv 自己的父目录搜索。
    显式传入 ``env_file`` 时则只使用该文件，路径错误会立即报告给调用方。

    ``override=False`` 是有意保留的：如果用户已经在 Shell 或 Conda 环境里设置了变量，
    外部环境变量优先级高于 ``.env``，不会被本地文件悄悄覆盖。
    """

    if env_file is not None:
        path = env_file.expanduser().resolve()
        if not path.is_file():
            raise ModelError(f"DeepSeek environment file does not exist: {path}")
        load_dotenv(dotenv_path=path, override=False)
        return path

    source_root = Path(__file__).resolve().parents[3]
    candidates = (Path.cwd() / ".env", source_root / ".env")
    for path in candidates:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            return path

    discovered = find_dotenv(usecwd=True)
    if discovered:
        path = Path(discovered).resolve()
        load_dotenv(dotenv_path=path, override=False)
        return path
    return None


def _configured_timeout(explicit: float | None) -> float:
    """把构造参数或环境变量转换成正数超时时间，并给出清晰的配置错误。"""

    if explicit is not None:
        timeout = explicit
    else:
        raw = os.getenv("DEEPSEEK_TIMEOUT_SECONDS")
        if raw is None or not raw.strip():
            return DEFAULT_TIMEOUT_SECONDS
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise ModelError("DEEPSEEK_TIMEOUT_SECONDS must be a number") from exc
    if timeout <= 0:
        raise ModelError("DEEPSEEK_TIMEOUT_SECONDS must be greater than zero")
    return timeout


class DeepSeekAdapter:
    """将统一 ModelRequest 转换为 DeepSeek Chat Completions 请求。

    配置优先级从高到低是：构造函数显式参数、当前进程环境变量、项目 ``.env`` 文件、
    内置默认值。API Key、base URL 和 timeout 都遵循这个规则，只有 API Key 没有任何
    可用值时才会拒绝初始化。密钥只保存在内存中，不会写入 Session 事件或异常文本。
    """

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        env_file: Path | None = None,
    ) -> None:
        """解析配置、保存连接参数，并在缺少 API Key 时 fail closed。"""

        self.env_file = _load_environment(env_file)
        configured_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.api_key = configured_key.strip() if configured_key else None
        configured_url = base_url or os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = configured_url.strip().rstrip("/")
        self.timeout_seconds = _configured_timeout(timeout_seconds)
        if not self.api_key:
            raise ModelError("DEEPSEEK_API_KEY is required for the deepseek provider")

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """发送一次非流式 Chat Completions 请求并转换响应。

        当前 AgentLoop 优先使用 stream；保留 complete 是为了兼容简单调用方和只实现
        非流式协议的测试适配器。
        """

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

    async def stream(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[ModelChunk]:
        """通过 DeepSeek 的 SSE 接口逐段返回模型输出。

        普通文本 delta 会立即 yield 给 AgentLoop，因此用户可以在终端看到模型逐步输出。
        DeepSeek 对工具调用会把 id、函数名和 JSON 参数拆成多个 delta；本方法只在收齐
        后拼成完整 ToolCall，避免 Agent 在参数尚未接收完整时提前执行工具。
        """

        if cancel_event.is_set():
            raise asyncio.CancelledError
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = request.tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        call_parts: dict[int, dict[str, str]] = {}
        finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
        usage: Usage | None = None

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if cancel_event.is_set():
                            raise asyncio.CancelledError
                        if not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            break
                        raw_event = json.loads(data)
                        if not isinstance(raw_event, Mapping):
                            raise ModelError("invalid DeepSeek stream event: expected an object")
                        raw_choices = raw_event.get("choices", [])
                        if not isinstance(raw_choices, list) or not raw_choices:
                            raw_usage = raw_event.get("usage")
                            if isinstance(raw_usage, Mapping):
                                usage = Usage.model_validate(raw_usage)
                            continue
                        choice = raw_choices[0]
                        if not isinstance(choice, Mapping):
                            raise ModelError(
                                "invalid DeepSeek stream event: choices[0] must be an object"
                            )
                        raw_delta = choice.get("delta", {})
                        if not isinstance(raw_delta, Mapping):
                            raise ModelError(
                                "invalid DeepSeek stream event: delta must be an object"
                            )
                        content = raw_delta.get("content")
                        if isinstance(content, str) and content:
                            yield ModelChunk(content=content)
                        self._accumulate_stream_calls(raw_delta.get("tool_calls"), call_parts)
                        raw_reason = choice.get("finish_reason")
                        if raw_reason in {"stop", "tool_calls", "length", "error"}:
                            finish_reason = raw_reason
                        raw_usage = raw_event.get("usage")
                        if isinstance(raw_usage, Mapping):
                            usage = Usage.model_validate(raw_usage)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ModelError(f"DeepSeek streaming request failed: {exc}") from exc

        calls = [
            self._finish_stream_call(index, parts) for index, parts in sorted(call_parts.items())
        ]
        yield ModelChunk(
            tool_calls=calls,
            finish_reason=finish_reason,
            usage=usage,
            done=True,
        )

    @staticmethod
    def _accumulate_stream_calls(
        raw_calls: object,
        call_parts: dict[int, dict[str, str]],
    ) -> None:
        """累积一个 SSE delta 中可能只包含部分内容的工具调用字段。"""

        if raw_calls is None:
            return
        if not isinstance(raw_calls, list):
            raise ModelError("invalid DeepSeek stream event: tool_calls must be a list")
        for fallback_index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                raise ModelError("invalid DeepSeek stream event: tool call must be an object")
            raw_index = raw_call.get("index", fallback_index)
            if not isinstance(raw_index, int):
                raise ModelError(
                    "invalid DeepSeek stream event: tool call index must be an integer"
                )
            parts = call_parts.setdefault(raw_index, {"id": "", "name": "", "arguments": ""})
            raw_id = raw_call.get("id")
            if isinstance(raw_id, str):
                parts["id"] += raw_id
            function = raw_call.get("function", {})
            if not isinstance(function, Mapping):
                raise ModelError("invalid DeepSeek stream event: function must be an object")
            raw_name = function.get("name")
            if isinstance(raw_name, str):
                parts["name"] += raw_name
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                parts["arguments"] += raw_arguments

    @staticmethod
    def _finish_stream_call(index: int, parts: dict[str, str]) -> ToolCall:
        """把累积的工具调用片段解析为可执行的完整 ToolCall。"""

        if not parts["id"] or not parts["name"]:
            raise ModelError(f"invalid DeepSeek stream tool call at index {index}")
        try:
            arguments = json.loads(parts["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            raise ModelError(
                f"invalid DeepSeek stream tool arguments at index {index}: {exc}"
            ) from exc
        if not isinstance(arguments, dict):
            raise ModelError(
                f"invalid DeepSeek stream tool arguments at index {index}: expected an object"
            )
        return ToolCall(id=CallId(parts["id"]), name=parts["name"], arguments=arguments)

    def _request(self, body: bytes) -> Mapping[str, object]:
        """同步执行一次 HTTP POST；调用方通过 asyncio.to_thread 避免阻塞事件循环。"""

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
            """把 Provider 动态值收窄为映射，统一生成带字段路径的 ModelError。"""

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
