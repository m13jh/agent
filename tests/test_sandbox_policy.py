"""SANBOX 权限、删除策略和容器边界回归测试。"""

from __future__ import annotations

import pytest

from python_agent.approval.service import CallbackApprovalService
from python_agent.config import AgentPreset
from python_agent.core.agent_loop import AgentLoop
from python_agent.ids import CallId, SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.types import ToolCall
from python_agent.tools.builtins import ApplyPatchTool, BashTool, DeleteFileTool, WriteFileTool
from python_agent.tools.capabilities import (
    FILESYSTEM_WORKSPACE_WRITE,
    NETWORK_INTERNET,
    PermissionLevel,
    capabilities_for_level,
)
from python_agent.tools.command_risk import CommandRiskAnalyzer
from python_agent.tools.container import ContainerExecTool, ContainerManager
from python_agent.tools.definition import FunctionTool, ToolCapabilities
from python_agent.tools.delete_policy import DeletePolicyEngine
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.runtime_env import RuntimeEnvironment
from python_agent.tools.sandbox import EnvironmentBlocked, SandboxRunner
from python_agent.tools.task_manifest import TaskFileManifest
from python_agent.tools.types import ToolContext


def _context(
    workspace,
    *,
    manifest=None,
    approval=None,
    network_mode=None,
    permission_level=None,
) -> ToolContext:
    return ToolContext(
        session_id=SessionId("sandbox-test"),
        workspace=workspace,
        permission_mode="workspace-write",
        permission_level=permission_level,
        network_mode=network_mode,
        task_manifest=manifest,
        approval_service=approval,
    )


def test_permission_levels_keep_network_independent() -> None:
    l1 = capabilities_for_level(PermissionLevel.L1, network_mode="disabled")
    l2 = capabilities_for_level(PermissionLevel.L2, network_mode="full")
    assert FILESYSTEM_WORKSPACE_WRITE in l1
    assert NETWORK_INTERNET not in l1
    assert NETWORK_INTERNET in l2


def test_command_risk_finds_exact_and_indeterminate_deletes() -> None:
    exact = CommandRiskAnalyzer.analyze("rm -f src/main.py")
    assert exact.delete_paths == ("src/main.py",)
    assert exact.unknown_delete_scope is False

    indeterminate = CommandRiskAnalyzer.analyze("rm -rf *")
    assert indeterminate.unknown_delete_scope is True
    assert indeterminate.batch is True

    nested = CommandRiskAnalyzer.analyze("bash -lc 'rm important.py'")
    assert nested.delete_paths == ("important.py",)


def test_command_risk_distinguishes_tool_versions_from_network_operations() -> None:
    assert CommandRiskAnalyzer.analyze("conda --version").network_required is False
    assert CommandRiskAnalyzer.analyze("npm --version").network_required is False
    assert CommandRiskAnalyzer.analyze("python --version").network_required is False
    assert CommandRiskAnalyzer.analyze("command -v conda").unknown_delete_scope is False
    assert CommandRiskAnalyzer.analyze("command rm important.txt").unknown_delete_scope is True
    assert CommandRiskAnalyzer.analyze("conda install cmake").network_required is True
    assert (
        CommandRiskAnalyzer.analyze(
            "python -c 'import urllib.request; urllib.request.urlopen(\"https://example.com\")'"
        ).network_required
        is True
    )


def test_container_manager_does_not_accept_host_escape_flags(tmp_path) -> None:
    launch = ContainerManager().build(
        "apt-get update",
        workspace=tmp_path,
        cwd=tmp_path,
        network_mode="disabled",
    )
    command = " ".join(launch.argv)
    assert "--network=none" in command
    assert "--privileged" not in command
    assert "--pid=host" not in command
    assert "/var/run/docker.sock" not in command
    assert "src=/," not in command


def test_disabled_network_is_environment_blocked_when_namespace_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        SandboxRunner,
        "_network_namespace_supported",
        staticmethod(lambda bubblewrap, timeout: False),
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/bwrap")
    with pytest.raises(EnvironmentBlocked, match="environment_blocked"):
        SandboxRunner().build(
            "ls",
            workspace=tmp_path,
            cwd=tmp_path,
            writable=False,
            network_mode="disabled",
        )


def test_runtime_environment_discovers_conda_and_nvm_without_home_mount(
    monkeypatch, tmp_path
) -> None:
    home = tmp_path / "home" / "tester"
    conda_base = home / "miniconda3"
    conda_env = conda_base / "envs" / "agent"
    node_version = home / ".nvm" / "versions" / "node" / "v24.0.0"
    for path in (
        conda_base / "condabin",
        conda_base / "bin",
        conda_env / "bin",
        conda_env / "conda-meta",
        node_version / "bin",
    ):
        path.mkdir(parents=True)
    conda_exe = conda_base / "bin" / "conda"
    python_exe = conda_env / "bin" / "python"
    node_exe = node_version / "bin" / "node"
    for executable in (conda_exe, python_exe, node_exe):
        executable.write_text("", encoding="utf-8")

    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("sys.prefix", str(conda_env))
    monkeypatch.setenv("CONDA_PREFIX", str(conda_env))
    monkeypatch.setenv("CONDA_EXE", str(conda_exe))
    monkeypatch.setattr(
        "shutil.which",
        lambda name: {
            "conda": str(conda_exe),
            "python": str(python_exe),
            "node": str(node_exe),
        }.get(name),
    )

    runtime = RuntimeEnvironment.discover(
        commands=("conda", "python", "node"),
        include_current_python=True,
    )

    assert conda_base in runtime.mounts
    assert node_version in runtime.mounts
    assert home not in runtime.mounts
    assert str(conda_env / "bin") in {str(path) for path in runtime.path_entries}
    assert str(node_version / "bin") in {str(path) for path in runtime.path_entries}
    assert ("CONDA_PREFIX", str(conda_env)) in runtime.environment


@pytest.mark.asyncio
async def test_existing_file_requires_approval_and_uses_soft_delete(tmp_path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("keep me", encoding="utf-8")
    call = ToolCall(id=CallId("delete"), name="delete_file", arguments={"path": target.name})
    denied = await ToolRuntime(ToolRegistry([DeleteFileTool()])).execute(
        call,
        _context(tmp_path),
    )
    assert denied.is_error is True
    assert target.exists()

    approved = await ToolRuntime(
        ToolRegistry([DeleteFileTool()]),
        approval_service=CallbackApprovalService(lambda request: True),
    ).execute(call, _context(tmp_path))
    assert approved.is_error is False
    assert not target.exists()
    trash = tmp_path / ".agent-trash" / "sandbox-test" / target.name
    assert trash.read_text(encoding="utf-8") == "keep me"


@pytest.mark.asyncio
async def test_manifest_temporary_file_can_be_deleted_without_approval(tmp_path) -> None:
    target = tmp_path / "output.tmp"
    target.write_text("temporary", encoding="utf-8")
    manifest = TaskFileManifest("task-1", tmp_path)
    manifest.record_created(target, temporary=True)
    result = await ToolRuntime(ToolRegistry([DeleteFileTool()])).execute(
        ToolCall(id=CallId("delete-temp"), name="delete_file", arguments={"path": target.name}),
        _context(tmp_path, manifest=manifest),
    )
    assert result.is_error is False
    assert not target.exists()
    assert not (tmp_path / ".agent-trash").exists()


@pytest.mark.asyncio
async def test_write_tool_registers_task_file_for_followup_delete(tmp_path) -> None:
    manifest = TaskFileManifest("task-2", tmp_path)
    context = _context(tmp_path, manifest=manifest)
    runtime = ToolRuntime(ToolRegistry([WriteFileTool(), DeleteFileTool()]))
    written = await runtime.execute(
        ToolCall(
            id=CallId("write-temp"),
            name="write_file",
            arguments={"path": "tmp/result.json", "content": "{}"},
        ),
        context,
    )
    assert written.is_error is False
    deleted = await runtime.execute(
        ToolCall(
            id=CallId("delete-temp-2"),
            name="delete_file",
            arguments={"path": "tmp/result.json"},
        ),
        context,
    )
    assert deleted.is_error is False
    assert not (tmp_path / "tmp" / "result.json").exists()


@pytest.mark.asyncio
async def test_write_file_at_new_workspace_root_does_not_fail_manifest_registration(
    tmp_path,
) -> None:
    workspace = tmp_path / "new-workspace"
    manifest = TaskFileManifest("task-root", workspace)
    result = await ToolRuntime(ToolRegistry([WriteFileTool()])).execute(
        ToolCall(
            id=CallId("write-root"),
            name="write_file",
            arguments={"path": "CMakeLists.txt", "content": "project(root)\n"},
        ),
        _context(workspace, manifest=manifest),
    )
    assert result.is_error is False
    assert (workspace / "CMakeLists.txt").read_text(encoding="utf-8") == "project(root)\n"
    assert manifest.generated_dirs == ()


@pytest.mark.asyncio
async def test_apply_patch_delete_requires_the_shared_policy(tmp_path) -> None:
    target = tmp_path / "source.py"
    target.write_text("print('keep')\n", encoding="utf-8")
    patch = f"*** Begin Patch\n*** Delete File: {target.name}\n*** End Patch"
    denied = await ToolRuntime(ToolRegistry([ApplyPatchTool()])).execute(
        ToolCall(id=CallId("patch-delete"), name="apply_patch", arguments={"patch": patch}),
        _context(tmp_path),
    )
    assert denied.is_error is True
    assert target.exists()


@pytest.mark.asyncio
async def test_network_capability_is_blocked_or_approved_independently(tmp_path) -> None:
    called: list[bool] = []
    network_tool = FunctionTool(
        name="network_tool",
        description="network test",
        parameters={"type": "object"},
        body=lambda arguments, context: called.append(True) or "ok",
        capabilities=ToolCapabilities(
            read_only=True,
            destructive=False,
            open_world=False,
            concurrency_safe=True,
            requires_approval=False,
            requires_network=True,
        ),
    )
    call = ToolCall(id=CallId("network"), name="network_tool", arguments={})
    denied = await ToolRuntime(ToolRegistry([network_tool])).execute(
        call,
        _context(tmp_path),
    )
    assert denied.is_error is True
    assert called == []

    approved = await ToolRuntime(
        ToolRegistry([network_tool]),
        approval_service=CallbackApprovalService(lambda request: True),
    ).execute(
        call,
        _context(
            tmp_path,
            approval=CallbackApprovalService(lambda request: True),
            network_mode="full",
            permission_level="L2",
        ),
    )
    assert approved.is_error is False
    assert called == [True]


@pytest.mark.asyncio
async def test_container_tool_requires_l3_and_gateway_approval(tmp_path) -> None:
    class FakeContainerManager:
        async def run(self, command, **kwargs):
            return {"command": command, "returncode": 0}

    tool = ContainerExecTool(FakeContainerManager())
    context = _context(tmp_path, approval=CallbackApprovalService(lambda request: True))
    context.permission_level = "L3"
    result = await ToolRuntime(
        ToolRegistry([tool]),
        approval_service=context.approval_service,
    ).execute(
        ToolCall(id=CallId("container"), name="container_exec", arguments={"command": "true"}),
        context,
    )
    assert result.is_error is False


@pytest.mark.asyncio
async def test_agent_loop_carries_manifest_between_write_and_delete_steps(tmp_path) -> None:
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [
                    {
                        "id": "write",
                        "name": "write_file",
                        "arguments": {"path": "tmp/generated.tmp", "content": "data"},
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {
                "tool_calls": [
                    {
                        "id": "delete",
                        "name": "delete_file",
                        "arguments": {"path": "tmp/generated.tmp"},
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "clean"},
        ]
    )
    from python_agent.tools.builtins import DeleteFileTool, WriteFileTool

    result = await AgentLoop(
        adapter,
        ToolRegistry([WriteFileTool(), DeleteFileTool()]),
        config=AgentPreset(workspace=tmp_path, permission_mode="workspace-write"),
    ).run("生成并清理临时文件")
    assert result.answer == "clean"
    assert not (tmp_path / "tmp" / "generated.tmp").exists()


@pytest.mark.asyncio
async def test_shell_delete_uses_same_policy_and_blocks_unknown_scope(tmp_path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("do not remove", encoding="utf-8")
    runtime = ToolRuntime(ToolRegistry([BashTool()]))
    result = await runtime.execute(
        ToolCall(
            id=CallId("shell-delete"),
            name="bash",
            arguments={"command": f"rm {target.name}"},
        ),
        _context(tmp_path),
    )
    assert result.is_error is True
    assert target.exists()
    assert DeletePolicyEngine(tmp_path).evaluate(".", recursive=True, batch=True).action == "deny"
    assert CommandRiskAnalyzer.analyze("rm -rf *").unknown_delete_scope is True
