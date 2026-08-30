"""阶段六进程内子 Agent 的生命周期、权限、深度和工具集成测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from python_agent.config import AgentPreset
from python_agent.core.agent_manager import AgentManager
from python_agent.errors import SubagentLimitError, SubagentPermissionError
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import CallId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall
from python_agent.session.jsonl_store import JsonlSessionStore
from python_agent.subagents.types import SubagentSpec
from python_agent.tools.builtins import EchoTool, ReadFileTool
from python_agent.tools.registry import ToolRegistry


def _config(tmp_path: Path, **updates) -> AgentPreset:
    """创建启用子 Agent 的测试 preset。"""

    values = {
        "id": "parent-preset",
        "workspace": tmp_path,
        "subagents_enabled": True,
        "max_delegation_depth": 2,
        "max_subagents": 8,
    }
    values.update(updates)
    return AgentPreset(**values)


async def test_child_has_independent_persisted_session_and_filtered_tools(tmp_path: Path) -> None:
    """验证 child Header、独立多 Turn Session、工具子集和父级结果注入。"""

    settled_event = asyncio.Event()
    bus = LiveEventBus()
    bus.subscribe("subagent/settled", lambda kind, data: settled_event.set())
    store = JsonlSessionStore(tmp_path / "state")
    manager = AgentManager(event_bus=bus, session_store=store)
    parent = await manager.create(
        FakeAdapter(),
        ToolRegistry([EchoTool(), ReadFileTool()]),
        config=_config(tmp_path),
    )

    child_id = await manager.subagents.start(
        parent,
        "第一轮子任务",
        SubagentSpec(description="只允许 echo", allowed_tools={"echo"}),
    )
    first = await manager.subagents.wait(parent, child_id)
    await asyncio.wait_for(settled_event.wait(), timeout=1)
    child = manager.get(child_id)

    assert first.answer == "Echo: 第一轮子任务"
    assert child.session.header.parent_session_id == parent.id
    assert child.session.header.origin == "subagent"
    assert child.session.header.delegation_depth == 1
    assert child.tools.names() == ("echo",)
    assert child.config.tools == ("echo",)
    assert (tmp_path / "state" / "sessions" / str(child_id) / "events.jsonl").is_file()
    assert any(
        item.content.startswith(f"[subagent/result child_id={child_id}")
        for item in parent.inbox.pending("next_step")
    )

    settled_event.clear()
    await manager.subagents.followup(parent, child_id, "第二轮子任务")
    second = await manager.subagents.wait(parent, child_id)
    await asyncio.wait_for(settled_event.wait(), timeout=1)

    assert second.answer == "Echo: 第二轮子任务"
    assert [event.type for event in child.session.events].count("turn/start") == 2
    assert child.session.next_turn() == 3
    infos = await manager.subagents.list_children(parent.id)
    assert len(infos) == 1
    assert infos[0].last_answer == "Echo: 第二轮子任务"
    await manager.shutdown()


async def test_child_tools_must_be_parent_subset_and_steps_cannot_increase(tmp_path: Path) -> None:
    """验证子 Agent 不能通过规格请求父级未授权工具或更高 Step 上限。"""

    manager = AgentManager()
    parent = await manager.create(
        FakeAdapter(),
        ToolRegistry([EchoTool()]),
        config=_config(tmp_path, max_steps=3),
    )

    with pytest.raises(SubagentPermissionError, match="subset"):
        await manager.subagents.start(
            parent,
            "越权工具",
            SubagentSpec(description="越权", allowed_tools={"read_file"}),
        )
    with pytest.raises(SubagentLimitError, match="max_steps"):
        await manager.subagents.start(
            parent,
            "越权步数",
            SubagentSpec(description="步数", allowed_tools={"echo"}, max_steps=4),
        )

    assert await manager.subagents.list_children(parent.id) == []
    await manager.shutdown()


async def test_only_direct_parent_can_control_child(tmp_path: Path) -> None:
    """验证兄弟根 Agent 不能 followup 或 interrupt 不属于自己的 child。"""

    manager = AgentManager()
    parent = await manager.create(
        FakeAdapter(), ToolRegistry([EchoTool()]), config=_config(tmp_path, id="parent-a")
    )
    stranger = await manager.create(
        FakeAdapter(), ToolRegistry([EchoTool()]), config=_config(tmp_path, id="parent-b")
    )
    child_id = await manager.subagents.start(
        parent,
        "合法子任务",
        SubagentSpec(description="child", allowed_tools={"echo"}),
    )
    await manager.subagents.wait(parent, child_id)

    with pytest.raises(SubagentPermissionError, match="not the direct parent"):
        await manager.subagents.followup(stranger, child_id, "非法 followup")
    with pytest.raises(SubagentPermissionError, match="not the direct parent"):
        await manager.subagents.interrupt(stranger, child_id)
    await manager.shutdown()


async def test_depth_limit_and_child_first_dispose(tmp_path: Path) -> None:
    """验证两层委派可用、第三层被拒绝，释放顺序为 grandchild → child。"""

    disposed: list[str] = []
    bus = LiveEventBus()
    bus.subscribe(
        "subagent/disposed",
        lambda kind, data: disposed.append(data["child_id"]),
    )
    manager = AgentManager(event_bus=bus)
    root = await manager.create(
        FakeAdapter(),
        ToolRegistry([EchoTool()]),
        config=_config(tmp_path, max_delegation_depth=2),
    )
    child_id = await manager.subagents.start(
        root,
        "第一层",
        SubagentSpec(description="child"),
    )
    await manager.subagents.wait(root, child_id)
    child = manager.get(child_id)
    assert "spawn_agent" in child.tools.names()

    grandchild_id = await manager.subagents.start(
        child,
        "第二层",
        SubagentSpec(description="grandchild"),
    )
    await manager.subagents.wait(child, grandchild_id)
    grandchild = manager.get(grandchild_id)
    assert grandchild.session.header.delegation_depth == 2
    assert "spawn_agent" not in grandchild.tools.names()

    with pytest.raises(SubagentLimitError, match="delegation depth 3"):
        await manager.subagents.start(
            grandchild,
            "第三层",
            SubagentSpec(description="too deep"),
        )

    await manager.dispose(root.id)
    assert disposed == [str(grandchild_id), str(child_id)]
    assert manager.list_agents() == []


async def test_direct_child_count_limit(tmp_path: Path) -> None:
    """验证一个父 Agent 的直接孩子数受 max_subagents 限制。"""

    manager = AgentManager()
    parent = await manager.create(
        FakeAdapter(),
        ToolRegistry([EchoTool()]),
        config=_config(tmp_path, max_subagents=1),
    )
    child_id = await manager.subagents.start(
        parent,
        "第一个",
        SubagentSpec(description="first", allowed_tools={"echo"}),
    )
    await manager.subagents.wait(parent, child_id)

    with pytest.raises(SubagentLimitError, match="already owns 1"):
        await manager.subagents.start(
            parent,
            "第二个",
            SubagentSpec(description="second", allowed_tools={"echo"}),
        )
    await manager.shutdown()


class BlockingAdapter:
    """用于验证父级 interrupt 可以收敛 child Driver 的阻塞模型。"""

    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """等待取消；正常情况下不会返回答案。"""

        del request
        self.started.set()
        await cancel_event.wait()
        raise asyncio.CancelledError


async def test_parent_can_interrupt_child_and_no_driver_is_left(tmp_path: Path) -> None:
    """验证 interrupt 清空 child Inbox、记录 aborted 并使其回到 idle。"""

    adapter = BlockingAdapter()
    manager = AgentManager()
    parent = await manager.create(
        adapter,
        ToolRegistry([EchoTool()]),
        config=_config(tmp_path),
    )
    child_id = await manager.subagents.start(
        parent,
        "阻塞任务",
        SubagentSpec(description="blocking", allowed_tools={"echo"}),
    )
    await adapter.started.wait()
    await manager.subagents.interrupt(parent, child_id)
    settled = await manager.subagents.wait(parent, child_id)
    child = manager.get(child_id)

    assert settled.finish_reason == "interrupted"
    assert child.status == "idle"
    assert child.inbox.pending() == ()
    assert [event for event in child.session.events if event.type == "turn/end"][-1].data[
        "reason"
    ] == "aborted"
    await manager.shutdown()


async def test_model_can_call_spawn_agent_tool_and_wait_for_answer(tmp_path: Path) -> None:
    """验证模型 → spawn_agent → child 模型 → tool/result → 父模型完整闭环。"""

    def responder(request: ModelRequest) -> AssistantResponse | dict:
        """根据最后消息区分父首次请求、child 请求和父工具后续请求。"""

        last_user = next(
            (
                message.get("content", "")
                for message in reversed(request.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if last_user == "调查子任务":
            return {"content": "子 Agent 的调查结果"}
        if any(message.get("role") == "tool" for message in request.messages):
            return {"content": "父 Agent 已收到子结果"}
        return {
            "tool_calls": [
                ToolCall(
                    id=CallId("spawn-1"),
                    name="spawn_agent",
                    arguments={
                        "prompt": "调查子任务",
                        "description": "调查",
                        "allowed_tools": ["echo"],
                        "wait": True,
                    },
                )
            ],
            "finish_reason": "tool_calls",
        }

    adapter = FakeAdapter(responder)
    manager = AgentManager()
    parent = await manager.create(
        adapter,
        ToolRegistry([EchoTool()]),
        config=_config(tmp_path),
    )
    result = await parent.run("请委派调查")

    assert result.answer == "父 Agent 已收到子结果"
    assert "spawn_agent" in parent.tools.names()
    tool_result = next(event for event in parent.session.events if event.type == "tool/result")
    assert tool_result.data["name"] == "spawn_agent"
    assert tool_result.data["content"]["answer"] == "子 Agent 的调查结果"
    infos = await manager.subagents.list_children(parent.id)
    assert len(infos) == 1
    assert infos[0].finish_reason == "stop"
    await manager.shutdown()
