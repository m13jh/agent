"""交互式 chat 的提示符清理、排队反馈和模型请求状态测试。"""

from __future__ import annotations

import asyncio

import pytest

from python_agent.cli import _dispatch_chat_line, _display_event, _normalize_chat_line
from python_agent.core.agent import Agent
from python_agent.core.agent_loop import AgentLoop
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.types import AssistantResponse, ModelRequest


@pytest.mark.parametrize(
    ("raw", "expected", "removed"),
    [
        ("你> /transcript", "/transcript", 1),
        ("你> 你> 我的问题", "我的问题", 2),
        ("   你>   /status   ", "/status", 1),
        ("普通输入", "普通输入", 0),
        ("   ", "", 0),
    ],
)
def test_normalize_chat_line_removes_copied_prompts(
    raw: str,
    expected: str,
    removed: int,
) -> None:
    """验证一个或多个误复制提示符被删除，普通文本保持不变。"""

    assert _normalize_chat_line(raw) == (expected, removed)


async def test_copied_transcript_prompt_is_dispatched_as_local_command(capsys) -> None:
    """验证 ``你> /transcript`` 不再触发模型，而是执行本地 transcript 命令。"""

    adapter = FakeAdapter()
    agent = Agent(adapter)

    should_exit = await _dispatch_chat_line(agent, "你> /transcript")
    output = capsys.readouterr().out

    assert should_exit is False
    assert "[输入修正]" in output
    assert "（当前没有 transcript）" in output
    assert adapter.requests == []
    assert agent.status == "idle"
    await agent.dispose()


class BlockingAdapter:
    """由测试控制释放时机的模型，用于稳定观察请求中状态与 followup 排队。"""

    name = "blocking"

    def __init__(self) -> None:
        """准备模型开始/释放信号和请求记录。"""

        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """阻塞第一条请求，让测试有机会提交第二条 followup 和查询状态。"""

        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return AssistantResponse(content=f"完成第 {len(self.requests)} 条")


async def test_running_followup_reports_queue_and_status_elapsed(capsys) -> None:
    """验证运行中输入明确显示已排队，/status 展示活动模型请求及等待时间。"""

    adapter = BlockingAdapter()
    agent = Agent(adapter)
    await _dispatch_chat_line(agent, "第一条任务")
    await adapter.started.wait()

    await _dispatch_chat_line(agent, "第二条任务")
    await _dispatch_chat_line(agent, "/status")
    output = capsys.readouterr().out

    assert "[已提交] followup 已唤醒 Agent" in output
    assert "[已排队] followup 已保存" in output
    assert "next_turn 当前 1 条" in output
    assert "状态：running" in output
    assert "模型请求：Turn 1 / Step 1" in output
    assert "fake/fake-model" in output
    assert "已等待" in output
    assert [item.content for item in agent.inbox.pending("next_turn")] == ["第二条任务"]

    adapter.release.set()
    await agent.when_idle()
    assert len(adapter.requests) == 2
    await agent.dispose()


async def test_agent_loop_publishes_model_request_duration_events() -> None:
    """验证请求开始/结束事件成对出现，活动状态在完成后被清空。"""

    adapter = BlockingAdapter()
    observed: list[tuple[str, dict]] = []
    loop = AgentLoop(adapter, event_handler=lambda kind, data: observed.append((kind, data)))
    task = asyncio.create_task(loop.run("检查状态"))
    await adapter.started.wait()

    active = loop.active_request
    assert active is not None
    assert active.turn == 1
    assert active.step == 1
    assert active.elapsed_seconds >= 0

    adapter.release.set()
    await task

    request_events = [item for item in observed if item[0].startswith("model/request_")]
    assert [kind for kind, _ in request_events] == ["model/request_start", "model/request_end"]
    assert request_events[1][1]["status"] == "completed"
    assert request_events[1][1]["finish_reason"] == "stop"
    assert request_events[1][1]["duration_ms"] >= 0
    assert loop.active_request is None


def test_display_event_renders_model_request_timing(capsys) -> None:
    """验证终端能看到模型名称、进行中状态和最终耗时。"""

    _display_event(
        "model/request_start",
        {"turn": 2, "step": 3, "provider": "deepseek", "model": "qwen3.7-flash"},
    )
    _display_event(
        "model/request_end",
        {
            "turn": 2,
            "step": 3,
            "provider": "deepseek",
            "model": "qwen3.7-flash",
            "status": "completed",
            "finish_reason": "stop",
            "duration_ms": 1234,
        },
    )
    output = capsys.readouterr().out

    assert "deepseek/qwen3.7-flash，正在等待响应" in output
    assert "用时 1.23 秒" in output
    assert "结束原因：stop" in output
