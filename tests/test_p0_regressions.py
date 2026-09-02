"""P0 安全边界和正确性回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path

import pytest

from python_agent.approval.service import CallbackApprovalService
from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.core.agent_loop import AgentLoop
from python_agent.core.agent_manager import AgentManager
from python_agent.errors import ConfigurationError, ModelError, ToolError
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import CallId, SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.types import AssistantResponse, ModelChunk, ModelRequest, ToolCall
from python_agent.session.events import SessionHeader
from python_agent.session.jsonl_store import JsonlSessionStore
from python_agent.subagents.types import SubagentSpec
from python_agent.tools.builtins import ApplyPatchTool, BashTool, ReadFileTool
from python_agent.tools.builtins._file_transaction import FileTransaction
from python_agent.tools.definition import FunctionTool, ToolCapabilities
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext

_READ_ONLY = ToolCapabilities(
    read_only=True,
    destructive=False,
    open_world=False,
    concurrency_safe=False,
    requires_approval=False,
)


class _IncompleteStreamAdapter:
    """发出一个看似完整的调用，但不发出流终止标记。"""

    name = "incomplete"

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        del request, cancel_event
        raise AssertionError("应当选择 stream 路径")

    async def stream(self, request: ModelRequest, *, cancel_event: asyncio.Event):
        del request, cancel_event
        yield ModelChunk(
            tool_calls=[ToolCall(id=CallId("incomplete-call"), name="side", arguments={})]
        )


class _LengthStreamAdapter:
    """发出携带工具调用的长度终止响应。"""

    name = "length"

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        del request, cancel_event
        raise AssertionError("应当选择 stream 路径")

    async def stream(self, request: ModelRequest, *, cancel_event: asyncio.Event):
        del request, cancel_event
        yield ModelChunk(
            tool_calls=[ToolCall(id=CallId("length-call"), name="side", arguments={})],
            finish_reason="length",
            done=True,
        )


def _side_effect_tool(counter: list[int]) -> FunctionTool:
    async def body(arguments: dict, context: ToolContext) -> str:
        del arguments, context
        counter.append(1)
        return "executed"

    return FunctionTool(
        name="side",
        description="test side effect",
        parameters={"type": "object"},
        body=body,
        capabilities=_READ_ONLY,
    )


@pytest.mark.parametrize(
    "adapter",
    [_IncompleteStreamAdapter(), _LengthStreamAdapter()],
    ids=["missing-done", "length"],
)
async def test_incomplete_or_length_stream_never_executes_tools(adapter) -> None:
    counter: list[int] = []
    loop = AgentLoop(
        adapter,
        ToolRegistry([_side_effect_tool(counter)]),
        config=AgentPreset(max_steps=1, model_max_retries=0),
    )

    if isinstance(adapter, _IncompleteStreamAdapter):
        with pytest.raises(ModelError, match="LLM_STREAM_CLOSED"):
            await loop.run("test")
    else:
        result = await loop.run("test")
        assert result.finish_reason == "length"
    assert counter == []


async def test_event_observer_failure_cannot_leave_agent_running() -> None:
    bus = LiveEventBus(observer_timeout_seconds=0.1)

    def broken_observer(event_type: str, data: dict) -> None:
        del event_type, data
        raise RuntimeError("观察者失败")

    bus.subscribe("agent/status", broken_observer)
    agent = Agent(FakeAdapter(), event_bus=bus)
    await agent.followup("hello")
    await asyncio.wait_for(agent.when_idle(), timeout=1)

    assert agent.status == "idle"
    assert any(error["error_type"] == "RuntimeError" for error in bus.observer_errors)
    await agent.dispose()


async def test_stalled_async_observer_is_bounded() -> None:
    bus = LiveEventBus(observer_timeout_seconds=0.01)

    async def stalled_observer(event_type: str, data: dict) -> None:
        del event_type, data
        await asyncio.sleep(1)

    bus.subscribe("agent/status", stalled_observer)
    agent = Agent(FakeAdapter(), event_bus=bus)
    await agent.followup("hello")
    await asyncio.wait_for(agent.when_idle(), timeout=1)

    assert agent.status == "idle"
    assert any(error["error_type"] == "TimeoutError" for error in bus.observer_errors)
    await agent.dispose()


async def test_bash_cannot_read_outside_workspace_even_when_approved(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-leak", encoding="utf-8")
    approval = CallbackApprovalService(lambda request: True)
    context = ToolContext(
        session_id=SessionId("bash-security"),
        workspace=workspace,
        permission_mode="workspace-write",
        approval_service=approval,
    )

    result = await ToolRuntime(
        ToolRegistry([BashTool()]),
        approval_service=approval,
    ).execute(
        ToolCall(
            id=CallId("bash-security"),
            name="bash",
            arguments={"command": f"cat {shlex.quote(str(outside))}"},
        ),
        context,
    )

    assert "must-not-leak" not in str(result.content)
    if not result.is_error:
        assert result.content["returncode"] != 0


async def test_apply_patch_rolls_back_when_later_target_is_invalid(tmp_path: Path) -> None:
    (tmp_path / "not-a-directory").write_text("file", encoding="utf-8")
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: first.txt",
            "+只有事务失效时才应该留下",
            "*** Add File: not-a-directory/second.txt",
            "+must fail",
            "*** End Patch",
        ]
    )

    result = await ToolRuntime(ToolRegistry([ApplyPatchTool()])).execute(
        ToolCall(id=CallId("patch"), name="apply_patch", arguments={"patch": patch}),
        ToolContext(
            session_id=SessionId("patch"),
            workspace=tmp_path,
            permission_mode="workspace-write",
        ),
    )

    assert result.is_error is True
    assert not (tmp_path / "first.txt").exists()


def test_file_transaction_rolls_back_after_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before", encoding="utf-8")
    transaction = FileTransaction(
        tmp_path,
        [(first, b"after"), (second, b"created")],
    )
    original_replace = os.replace

    def fail_second(source, destination):
        if Path(destination) == second:
            raise OSError("模拟提交失败")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(ToolError, match="模拟提交失败"):
        transaction.commit()

    assert first.read_text(encoding="utf-8") == "before"
    assert not second.exists()


def test_file_transaction_recovers_a_crash_tail_before_next_file_operation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("new", encoding="utf-8")
    transaction = tmp_path / ".python-agent" / "transactions" / "tx-crashed"
    transaction.mkdir(parents=True)
    (transaction / "0.backup").write_bytes(b"old")
    (transaction / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "changes": [
                    {
                        "path": "target.txt",
                        "existed": True,
                        "mode": 0o600,
                        "backup": "0.backup",
                        "new_sha256": "invalid-for-existing-restore",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    FileTransaction.recover_pending(tmp_path)

    assert target.read_text(encoding="utf-8") == "old"
    assert not transaction.exists()


async def test_tool_result_is_normalized_before_session_append(tmp_path: Path) -> None:
    tool = FunctionTool(
        name="path_result",
        description="返回 Path",
        parameters={"type": "object"},
        body=lambda arguments, context: tmp_path / "result.txt",
        capabilities=_READ_ONLY,
    )
    adapter = FakeAdapter(
        [{"tool_calls": [{"id": "path", "name": "path_result", "arguments": {}}]}]
    )
    result = await AgentLoop(
        adapter,
        ToolRegistry([tool]),
        config=AgentPreset(max_steps=1),
    ).run("返回路径")

    tool_result = next(event for event in result.session.events if event.type == "tool/result")
    assert tool_result.data["content"] == str(tmp_path / "result.txt")


async def test_unknown_function_tool_is_denied_in_read_only_mode(tmp_path: Path) -> None:
    called = False

    async def body(arguments: dict, context: ToolContext) -> str:
        nonlocal called
        del arguments, context
        called = True
        return "executed"

    tool = FunctionTool(
        name="delete_data",
        description="危险扩展",
        parameters={"type": "object"},
        body=body,
    )
    result = await ToolRuntime(ToolRegistry([tool])).execute(
        ToolCall(id=CallId("danger"), name="delete_data", arguments={}),
        ToolContext(session_id=SessionId("danger"), workspace=tmp_path),
    )

    assert result.is_error is True
    assert called is False


async def test_child_inherits_parent_exclusions_and_approval(tmp_path: Path) -> None:
    private = tmp_path / "private.txt"
    private.write_text("private", encoding="utf-8")
    approval = CallbackApprovalService(lambda request: True)
    manager = AgentManager()
    parent = await manager.create(
        FakeAdapter(),
        ToolRegistry([ReadFileTool()]),
        config=AgentPreset(id="cap-parent", workspace=tmp_path, subagents_enabled=True),
        excluded_paths=(private,),
        approval_service=approval,
    )

    child_id = await manager.subagents.start(
        parent,
        "child",
        SubagentSpec(description="child", allowed_tools={"read_file"}),
    )
    await manager.subagents.wait(parent, child_id)
    child = manager.get(child_id)
    context = ToolContext(
        session_id=child.id,
        workspace=tmp_path,
        permission_mode=child.config.permission_mode,
        excluded_paths=child.loop.excluded_paths,
    )
    result = await child.loop._runtime.execute(
        ToolCall(id=CallId("read"), name="read_file", arguments={"path": private.name}),
        context,
    )

    assert private.resolve() in child.loop.excluded_paths
    assert result.is_error is True
    assert child.loop._runtime.approval_service is approval
    await manager.shutdown()


async def test_fork_from_child_resets_delegation_lineage(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "state")
    source = await store.create(
        SessionHeader(
            id=SessionId("child"),
            cwd=tmp_path,
            parent_session_id=SessionId("parent"),
            origin="subagent",
            delegation_depth=2,
        )
    )

    forked = await store.fork(source.id, SessionId("branch"))

    assert forked.header.parent_session_id is None
    assert forked.header.origin == "user"
    assert forked.header.delegation_depth == 0
    assert forked.header.forked_from_session_id == source.id


async def test_resume_rejects_changed_capability_snapshot(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "state")
    manager = AgentManager(session_store=store)
    original = AgentPreset(id="fingerprint", workspace=tmp_path, max_tokens=2048)
    await manager.create(FakeAdapter(), config=original)
    session_id = manager.list_agents()[0].id
    await manager.dispose(session_id)

    changed = AgentPreset(id=original.id, workspace=tmp_path, max_tokens=8192)
    with pytest.raises(ConfigurationError, match="capability fingerprint"):
        await manager.resume(session_id, FakeAdapter(), config=changed)


async def test_synchronous_subagent_result_is_delivered_once(tmp_path: Path) -> None:
    def respond(request: ModelRequest):
        last_user = next(
            (
                message.get("content", "")
                for message in reversed(request.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if last_user == "child task":
            return {"content": "CHILD_RESULT"}
        if any(message.get("role") == "tool" for message in request.messages):
            return {"content": "PARENT_DONE"}
        return {
            "tool_calls": [
                {
                    "id": "spawn",
                    "name": "spawn_agent",
                    "arguments": {
                        "prompt": "child task",
                        "description": "child",
                        "allowed_tools": [],
                        "wait": True,
                    },
                }
            ],
            "finish_reason": "tool_calls",
        }

    manager = AgentManager()
    parent = await manager.create(
        FakeAdapter(respond),
        ToolRegistry([]),
        config=AgentPreset(id="wait-parent", workspace=tmp_path, subagents_enabled=True),
    )

    await parent.run("parent task")
    messages = parent.session.messages()
    child_mentions = sum("CHILD_RESULT" in str(message.get("content", "")) for message in messages)

    assert child_mentions == 1
    assert [message["role"] for message in messages] == ["user", "assistant", "tool", "assistant"]
    await manager.shutdown()
