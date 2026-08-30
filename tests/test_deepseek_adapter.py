"""DeepSeek Adapter 的配置和严格 SSE 完整性测试。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from python_agent.errors import ModelError
from python_agent.llm.deepseek_adapter import DeepSeekAdapter
from python_agent.llm.types import ModelChunk, ModelRequest


def test_deepseek_adapter_reads_connection_settings_from_env_file(
    tmp_path: Path, monkeypatch
) -> None:
    """验证 API Key、base URL 和 timeout 都来自显式指定的 .env 文件。"""

    # 清掉外部环境，确保这个测试验证的确实是 dotenv 文件，而不是当前 Shell 的同名变量。
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_TIMEOUT_SECONDS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=file-key\n"
        "DEEPSEEK_BASE_URL=https://example.invalid/v1\n"
        "DEEPSEEK_TIMEOUT_SECONDS=15\n",
        encoding="utf-8",
    )

    adapter = DeepSeekAdapter(env_file=env_file)

    assert adapter.api_key == "file-key"
    assert adapter.base_url == "https://example.invalid/v1"
    assert adapter.timeout_seconds == 15.0
    assert adapter.env_file == env_file.resolve()


def _request() -> ModelRequest:
    """创建不含工具 schema 的最小流式请求。"""

    return ModelRequest(
        provider="deepseek",
        model="test-model",
        system="test",
        messages=[{"role": "user", "content": "hello"}],
    )


def _transport(body: str, *, status: int = 200) -> httpx.MockTransport:
    """返回固定 SSE/HTTP 响应的异步 MockTransport。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            status,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    return httpx.MockTransport(handler)


def _event(value: dict) -> str:
    """编码一条完整 SSE data frame。"""

    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


async def _chunks(adapter: DeepSeekAdapter) -> list[ModelChunk]:
    """收集一次适配器 stream 的所有标准化片段。"""

    return [chunk async for chunk in adapter.stream(_request(), cancel_event=asyncio.Event())]


async def test_stream_requires_done_marker() -> None:
    """验证 HTTP 正常关闭但缺少 [DONE] 时产生结构化错误。"""

    body = _event({"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]})
    adapter = DeepSeekAdapter(
        api_key="test",
        base_url="https://example.invalid",
        transport=_transport(body),
    )

    with pytest.raises(ModelError, match="LLM_STREAM_CLOSED"):
        await _chunks(adapter)


async def test_stream_with_done_and_finish_reason_succeeds() -> None:
    """验证完整 finish_reason + DONE 生成最终 done chunk。"""

    body = (
        _event({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        + "data: [DONE]\n\n"
    )
    adapter = DeepSeekAdapter(
        api_key="test",
        base_url="https://example.invalid",
        transport=_transport(body),
    )
    chunks = await _chunks(adapter)

    assert chunks[0].content == "ok"
    assert chunks[-1].done is True
    assert chunks[-1].finish_reason == "stop"


async def test_length_finish_drops_incomplete_tool_arguments() -> None:
    """验证输出截断时不解析、更不执行半截工具参数。"""

    body = (
        _event(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":"x","content":"',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "length",
                    }
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    adapter = DeepSeekAdapter(
        api_key="test",
        base_url="https://example.invalid",
        transport=_transport(body),
    )
    chunks = await _chunks(adapter)

    assert chunks[-1].finish_reason == "length"
    assert chunks[-1].tool_calls == []


async def test_http_auth_error_has_structured_code() -> None:
    """验证认证错误可被默认重试策略识别为永久失败。"""

    adapter = DeepSeekAdapter(
        api_key="test",
        base_url="https://example.invalid",
        transport=_transport("unauthorized", status=401),
    )

    with pytest.raises(ModelError, match="LLM_HTTP_401"):
        await _chunks(adapter)
