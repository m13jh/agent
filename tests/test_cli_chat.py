"""交互式 chat 的提示符清理、排队反馈和模型请求状态测试。"""

from __future__ import annotations

import asyncio

import pytest

from python_agent.approval.service import ApprovalRequest, InteractiveApprovalService
from python_agent.cli import _dispatch_chat_line, _display_event, _normalize_chat_line
from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.core.agent_loop import AgentLoop
from python_agent.ids import CallId, SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall
from python_agent.tools.builtins import BashTool, EchoTool
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext


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


async def test_interactive_approval_waits_for_chat_reply() -> None:
    """交互审批必须暂停工具，直到 chat 输入循环提交明确的 y/n。"""

    requests: list[ApprovalRequest] = []
    approval = InteractiveApprovalService(requests.append)
    pending = asyncio.create_task(
        approval.request(
            ApprovalRequest(
                call_id=CallId("approval"),
                tool_name="bash",
                arguments={"command": "touch created.txt"},
                reason="tool bash requires explicit approval",
            )
        )
    )
    await asyncio.sleep(0)

    assert approval.has_pending is True
    assert len(requests) == 1
    assert pending.done() is False
    assert approval.respond(True) is True
    assert await pending is True


async def test_chat_routes_no_to_interactive_approval(capsys) -> None:
    """待审批时输入 no 应该消费本地决定，而不是发送一个新的模型 followup。"""

    approval = InteractiveApprovalService(lambda request: None)
    pending = asyncio.create_task(
        approval.request(
            ApprovalRequest(
                call_id=CallId("approval-no"),
                tool_name="delete_file",
                arguments={"path": "important.txt"},
                reason="existing user file",
            )
        )
    )
    await asyncio.sleep(0)
    agent = Agent(FakeAdapter())
    try:
        assert await _dispatch_chat_line(agent, "no", approval_service=approval) is False
        assert await pending is False
        assert "已拒绝" in capsys.readouterr().out
    finally:
        await agent.dispose()


async def test_interactive_approval_blocks_real_bash_tool_until_reply(tmp_path, capsys) -> None:
    """真实 Bash 工具未收到 chat 回复前不得执行，no 后目标文件仍不存在。"""

    approval = InteractiveApprovalService(lambda request: None)
    context = ToolContext(
        session_id=SessionId("approval-session"),
        workspace=tmp_path,
        permission_mode="workspace-write",
        permission_level="L1",
        network_mode="disabled",
    )
    task = asyncio.create_task(
        ToolRuntime(ToolRegistry([BashTool()]), approval_service=approval).execute(
            ToolCall(
                id=CallId("approval-bash"),
                name="bash",
                arguments={"command": "touch approval_should_not_exist.txt"},
            ),
            context,
        )
    )
    await asyncio.sleep(0)
    agent = Agent(FakeAdapter())
    try:
        assert approval.has_pending is True
        assert await _dispatch_chat_line(agent, "no", approval_service=approval) is False
        result = await task
        assert result.is_error is True
        assert not (tmp_path / "approval_should_not_exist.txt").exists()
        assert "已拒绝" in capsys.readouterr().out
    finally:
        await agent.dispose()


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


async def test_max_steps_pauses_agent_without_final_answer() -> None:
    """达到 max_steps 后 Driver 虽回到 idle，但最近任务必须是 paused。"""

    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "echo-1", "name": "echo", "arguments": {"value": "partial"}}],
                "finish_reason": "tool_calls",
            }
        ]
    )
    agent = Agent(
        adapter,
        ToolRegistry([EchoTool()]),
        config=AgentPreset(max_steps=1),
    )
    limits: list[dict] = []
    agent.event_bus.subscribe("agent/limit", lambda kind, data: limits.append(data))

    result = await agent.run("需要继续的任务")

    assert agent.status == "idle"
    assert agent.task_status == "paused"
    assert result.task_status == "paused"
    assert result.answer == ""
    assert result.finish_reason == "max_steps"
    assert limits[0]["reason"] == "max_steps"
    assert "尚未生成最终回答" in limits[0]["message"]
    await agent.dispose()


async def test_continue_resumes_paused_task_and_completes(capsys) -> None:
    """/continue 应沿用已有上下文继续任务，并在最终回答后变为 completed。"""

    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "echo-1", "name": "echo", "arguments": {"value": "partial"}}],
                "finish_reason": "tool_calls",
            },
            {"content": "继续后的最终答案", "finish_reason": "stop"},
        ]
    )
    agent = Agent(
        adapter,
        ToolRegistry([EchoTool()]),
        config=AgentPreset(max_steps=1),
    )

    await agent.run("需要继续的任务")
    await _dispatch_chat_line(agent, "/continue")
    await agent.when_idle()

    assert capsys.readouterr().out == "[已提交] 正在继续上一个尚未完成的任务。\n"
    assert agent.status == "idle"
    assert agent.task_status == "completed"
    assert agent.last_result is not None
    assert agent.last_result.answer == "继续后的最终答案"
    assert len(adapter.requests) == 2
    await agent.dispose()


async def test_new_followup_warns_when_previous_task_was_paused(capsys) -> None:
    """暂停后输入普通文本会开启新 Turn，但明确提示上一个任务未完成。"""

    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "echo-1", "name": "echo", "arguments": {"value": "partial"}}],
                "finish_reason": "tool_calls",
            },
            {"content": "新任务答案", "finish_reason": "stop"},
        ]
    )
    agent = Agent(
        adapter,
        ToolRegistry([EchoTool()]),
        config=AgentPreset(max_steps=1),
    )

    await agent.run("第一个未完成任务")
    await _dispatch_chat_line(agent, "另一个新任务")
    output = capsys.readouterr().out

    assert "上一个任务因执行限制暂停" in output
    assert "尚未生成最终回答" in output
    assert "/continue" in output
    await agent.when_idle()
    assert agent.last_result is not None
    assert agent.last_result.answer == "新任务答案"
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
