import asyncio
from pathlib import Path

from python_agent.approval.service import CallbackApprovalService
from python_agent.ids import SessionId
from python_agent.llm.types import ToolCall
from python_agent.tools.builtins import ApplyPatchTool, BashTool, WriteFileTool
from python_agent.tools.definition import FunctionTool
from python_agent.tools.policies import ToolInvocation, ToolResultEnvelope
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext


async def test_agent_can_self_correct_after_pre_policy_error(tmp_path: Path) -> None:
    """验证 Pre 策略错误会进入下一模型请求，而不是执行越界读取。"""

    from python_agent.config import AgentPreset
    from python_agent.core.agent_loop import AgentLoop
    from python_agent.llm.fake_adapter import FakeAdapter
    from python_agent.tools.builtins import ReadFileTool

    adapter = FakeAdapter(
        [
            {
                "tool_calls": [
                    {
                        "id": "outside",
                        "name": "read_file",
                        "arguments": {"path": "../secret.txt"},
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "我看到了路径错误，改用安全路径。"},
        ]
    )
    result = await AgentLoop(
        adapter,
        ToolRegistry([ReadFileTool()]),
        config=AgentPreset(workspace=tmp_path),
    ).run("读取文件")

    assert result.answer == "我看到了路径错误，改用安全路径。"
    assert "outside workspace" in adapter.requests[1].messages[-1]["content"]


def _context(workspace: Path, *, writable: bool = False) -> ToolContext:
    """创建指定 workspace 和权限模式的工具上下文，减少测试样板代码。"""

    return ToolContext(
        session_id=SessionId("stage3"),
        workspace=workspace,
        permission_mode="workspace-write" if writable else "read-only",
    )


async def test_write_and_apply_patch_tools_use_workspace_write_mode(tmp_path: Path) -> None:
    """验证完整写入和结构化补丁都能在 workspace-write 模式完成。"""

    context = _context(tmp_path, writable=True)
    runtime = ToolRuntime(ToolRegistry([WriteFileTool(), ApplyPatchTool()]))

    written = await runtime.execute(
        ToolCall(id="write", name="write_file", arguments={"path": "note.txt", "content": "old\n"}),
        context,
    )
    assert written.is_error is False
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "old\n"

    patch = "*** Begin Patch\n*** Update File: note.txt\n@@\n-old\n+new\n*** End Patch"
    patched = await runtime.execute(
        ToolCall(id="patch", name="apply_patch", arguments={"patch": patch}),
        context,
    )
    assert patched.is_error is False
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "new\n"


async def test_workspace_policy_rejects_before_tool_body(tmp_path: Path) -> None:
    """验证越界路径在自定义工具主体执行前被拒绝。"""

    called = False

    async def body(arguments: dict, context: ToolContext) -> str:
        """记录主体是否被调用；策略拒绝时这里不应执行。"""

        nonlocal called
        called = True
        return "should not run"

    tool = FunctionTool(
        name="read_file",
        description="test",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        body=body,
    )
    result = await ToolRuntime(ToolRegistry([tool])).execute(
        ToolCall(id="outside", name="read_file", arguments={"path": "../secret.txt"}),
        _context(tmp_path),
    )

    assert result.is_error is True
    assert "outside workspace" in str(result.content)
    assert called is False


async def test_bash_requires_approval_before_execution(tmp_path: Path) -> None:
    """验证 Bash 无审批时拒绝，有审批时运行安全的 printf 命令。"""

    call = ToolCall(id="bash", name="bash", arguments={"command": "printf stage3"})
    context = _context(tmp_path, writable=True)

    denied = await ToolRuntime(ToolRegistry([BashTool()])).execute(call, context)
    assert denied.is_error is True
    assert "approval denied" in str(denied.content)

    approved_runtime = ToolRuntime(
        ToolRegistry([BashTool()]),
        approval_service=CallbackApprovalService(lambda request: True),
    )
    approved = await approved_runtime.execute(call, context)
    assert approved.is_error is False
    assert approved.content["stdout"] == "stage3"


async def test_tool_timeout_becomes_error_result(tmp_path: Path) -> None:
    """验证超时会变成 is_error 结果，而不是泄漏未处理异常。"""

    async def slow_body(arguments: dict, context: ToolContext) -> str:
        """故意超过工具超时，稳定触发 TimeoutPolicy。"""

        await asyncio.sleep(0.05)
        return "finished"

    tool = FunctionTool(
        name="slow",
        description="test",
        parameters={"type": "object"},
        body=slow_body,
        timeout_seconds=0.001,
    )
    result = await ToolRuntime(ToolRegistry([tool])).execute(
        ToolCall(id="slow", name="slow", arguments={}),
        _context(tmp_path),
    )

    assert result.is_error is True
    assert "TimeoutError" in str(result.content)


async def test_pre_execute_post_waterfalls_run_in_order(tmp_path: Path) -> None:
    """验证 Pre、Execute、业务主体和 Post 的进入/退出顺序。"""

    order: list[str] = []

    async def body(arguments: dict, context: ToolContext) -> str:
        """记录业务工具主体在策略链中的位置。"""

        order.append("body")
        return "ok"

    async def pre(
        invocation: ToolInvocation,
        next_handler,
    ):
        """记录 Pre 策略委托前后的两个观察点。"""

        order.append("pre-before")
        result = await next_handler(invocation)
        order.append("pre-after")
        return result

    async def execute(invocation: ToolInvocation, next_handler):
        """记录 Execute 策略包裹工具主体的前后顺序。"""

        order.append("execute-before")
        result = await next_handler(invocation)
        order.append("execute-after")
        return result

    async def post(envelope: ToolResultEnvelope, next_handler):
        """记录 Post 策略处理最终 ToolResult 的前后顺序。"""

        order.append("post-before")
        result = await next_handler(envelope)
        order.append("post-after")
        return result

    tool = FunctionTool(
        name="ordered",
        description="test",
        parameters={"type": "object"},
        body=body,
    )
    result = await ToolRuntime(
        ToolRegistry([tool]),
        pre_policies=(pre,),
        execute_policies=(execute,),
        post_policies=(post,),
    ).execute(
        ToolCall(id="ordered", name="ordered", arguments={}),
        _context(tmp_path),
    )

    assert result.content == "ok"
    assert order == [
        "pre-before",
        "pre-after",
        "execute-before",
        "body",
        "execute-after",
        "post-before",
        "post-after",
    ]


async def test_long_result_is_spilled_and_model_receives_summary(tmp_path: Path) -> None:
    """验证长结果保存到 spill 文件，模型只收到摘要信息。"""

    tool = FunctionTool(
        name="long",
        description="test",
        parameters={"type": "object"},
        body=lambda arguments, context: "x" * 40,
    )
    result = await ToolRuntime(
        ToolRegistry([tool]),
        max_result_chars=10,
        spill_directory=tmp_path / "spill",
    ).execute(
        ToolCall(id="long", name="long", arguments={}),
        _context(tmp_path),
    )

    assert result.is_error is False
    assert result.content["truncated"] is True
    spilled_path = Path(result.content["spilled_path"])
    assert spilled_path.read_text(encoding="utf-8") == "x" * 40
