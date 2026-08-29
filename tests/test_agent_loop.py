"""阶段 1 Agent Loop 的集成测试。

测试使用 FakeAdapter 固定模型输出，专门验证工具调用、下一 Step、错误结果和流式事件；
这样不会依赖真实 API Key，也能稳定检查 Session 事件和模型请求内容。
"""

from pathlib import Path

from python_agent.config import AgentPreset
from python_agent.core.agent_loop import AgentLoop
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.tools.builtins import EchoTool, ReadFileTool
from python_agent.tools.registry import ToolRegistry


async def test_fake_model_tool_model_loop(tmp_path: Path) -> None:
    """验证模型先读取文件，再根据 tool result 生成最终回答。"""

    (tmp_path / "note.txt").write_text("hello from file\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "read-1", "name": "read_file", "arguments": {"path": "note.txt"}}
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "最终答案：hello from file", "finish_reason": "stop"},
        ]
    )
    result = await AgentLoop(
        adapter,
        ToolRegistry([ReadFileTool()]),
        config=AgentPreset(workspace=tmp_path),
    ).run("请读取 note.txt")

    assert result.answer == "最终答案：hello from file"
    assert [event.seq for event in result.session.events] == list(range(len(result.session.events)))
    assert [event.type for event in result.session.events].count("tool/call") == 1
    assert [event.type for event in result.session.events].count("tool/result") == 1
    assert len(adapter.requests) == 2
    assert adapter.requests[1].messages[-1]["role"] == "tool"
    assert adapter.requests[1].messages[-1]["content"] == "hello from file\n"


async def test_multiple_tool_calls_are_executed_serially() -> None:
    """验证同一模型响应中的多个工具调用按返回顺序执行和写入日志。"""

    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "echo-1", "name": "echo", "arguments": {"value": "one"}},
                    {"id": "echo-2", "name": "echo", "arguments": {"value": "two"}},
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "one and two", "finish_reason": "stop"},
        ]
    )
    result = await AgentLoop(adapter, ToolRegistry([EchoTool()])).run("combine")

    results = [event for event in result.session.events if event.type == "tool/result"]
    assert [event.data["call_id"] for event in results] == ["echo-1", "echo-2"]
    assert [
        message["content"] for message in adapter.requests[1].messages if message["role"] == "tool"
    ] == [
        "one",
        "two",
    ]


async def test_tool_error_is_visible_to_next_model_step() -> None:
    """验证参数校验失败会变成下一次模型请求可见的错误消息。"""

    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "bad-1", "name": "echo", "arguments": {}}],
                "finish_reason": "tool_calls",
            },
            {"content": "I recovered from the tool error"},
        ]
    )
    result = await AgentLoop(adapter, ToolRegistry([EchoTool()])).run("try it")

    assert result.answer == "I recovered from the tool error"
    assert adapter.requests[1].messages[-1]["content"].startswith("ToolValidationError:")


async def test_streaming_adapter_emits_deltas_and_tool_events() -> None:
    """验证 FakeAdapter 的增量文本和工具生命周期事件都能实时通知调用方。"""

    adapter = FakeAdapter(
        [
            {
                "content": "先查看目录。",
                "tool_calls": [{"id": "list-1", "name": "echo", "arguments": {"value": "ok"}}],
                "finish_reason": "tool_calls",
            },
            {"content": "目录检查完成。", "finish_reason": "stop"},
        ]
    )
    live_events: list[tuple[str, dict]] = []

    def collect(event_type: str, data: dict) -> None:
        """保存实时事件，供测试按类型和顺序检查流式输出。"""

        live_events.append((event_type, data))

    result = await AgentLoop(
        adapter,
        ToolRegistry([EchoTool()]),
        event_handler=collect,
    ).run("检查")

    delta_text = "".join(data["content"] for kind, data in live_events if kind == "assistant/delta")
    assert delta_text == "先查看目录。目录检查完成。"
    assert [kind for kind, _ in live_events if kind in {"tool/call", "tool/result"}] == [
        "tool/call",
        "tool/result",
    ]
    assert result.answer == "目录检查完成。"
