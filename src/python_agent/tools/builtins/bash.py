"""受审批、限时并可回收进程组的 Bash 工具。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._file_transaction import FileTransaction
from python_agent.tools.builtins._paths import safe_path, workspace_root
from python_agent.tools.command_risk import CommandRiskAnalyzer
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.delete_policy import DeleteDecision, DeletePolicyEngine
from python_agent.tools.sandbox import SandboxRunner, SandboxSpec
from python_agent.tools.task_manifest import TaskFileManifest
from python_agent.tools.types import ToolContext


class BashTool:
    """在 workspace 下运行 Bash 命令；是否允许由 Pre 审批策略决定。

    每次调用都是一个新的 ``bash -lc`` 进程，命令不会自动继承上一次调用的 cwd、Shell
    变量或函数；API Key 等敏感环境变量也会在创建子进程前移除。
    """

    name = "bash"
    description = "Run a shell command in the workspace after explicit approval."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command passed to bash -lc"},
            "cwd": {
                "type": "string",
                "description": "Workspace-relative working directory",
                "default": ".",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 60.0
    handles_own_timeout = True
    capabilities = ToolCapabilities(
        read_only=False,
        destructive=True,
        open_world=True,
        concurrency_safe=False,
        requires_approval=True,
    )

    def __init__(self, sandbox_runner: SandboxRunner | None = None) -> None:
        """创建必须经过操作系统隔离的 Bash 工具。"""

        self.sandbox_runner = sandbox_runner or SandboxRunner()

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """Shell 可能读写任意 workspace 状态，默认不允许与其他工具重叠。"""

        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """启动命令、异步等待结束并返回 stdout/stderr/退出码。

        进程创建使用 Popen，输出写临时文件，主协程以 poll 加 sleep 轮询；这样既避开
        部分 WSL 环境的 asyncio child watcher 问题，也能在长命令期间响应取消和超时。
        """

        command = arguments["command"].strip()
        if not command:
            raise ToolError("bash command cannot be empty")
        if not CommandRiskAnalyzer.is_explicit_read_only(command):
            approved_tools = context.metadata.get("__approved_tool_names__", set())
            if not isinstance(approved_tools, set) or self.name not in approved_tools:
                raise ToolError("approval denied: Bash must pass the Policy Gateway")
            approved_tools.discard(self.name)
        root = workspace_root(context)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"cannot initialize workspace: {exc}") from exc
        FileTransaction.recover_pending(root)
        cwd = safe_path(arguments.get("cwd", "."), context)
        if not cwd.is_dir():
            raise ToolError(f"bash cwd is not a directory: {arguments.get('cwd', '.')}")
        risk = CommandRiskAnalyzer.analyze(command)
        if risk.reserved_path:
            raise ToolError("permission denied: agent infrastructure paths are not available")
        if risk.host_admin:
            raise ToolError(
                "host-admin command denied: ordinary Agent tools cannot modify the host"
            )
        if risk.unknown_delete_scope:
            raise ToolError(
                "delete denied: Shell deletion target scope is not fully determinable; "
                "use delete_file/delete_directory with explicit paths"
            )
        if risk.delete_paths:
            engine_candidate = context.delete_policy
            engine = (
                engine_candidate
                if isinstance(engine_candidate, DeletePolicyEngine)
                else DeletePolicyEngine(
                    workspace=root,
                    manifest=context.task_manifest,
                    approval_service=context.approval_service,
                )
            )
            command_cwd = safe_path(str(arguments.get("cwd", ".")), context)
            resolved_delete_paths = [
                path if Path(path).is_absolute() else str(command_cwd / path)
                for path in risk.delete_paths
            ]
            stored = context.metadata.get("__delete_decisions__", {})
            decision: DeleteDecision | None = None
            if len(resolved_delete_paths) == 1 and isinstance(stored, dict):
                candidate = stored.get(str(Path(resolved_delete_paths[0]).resolve()))
                if isinstance(candidate, DeleteDecision):
                    decision = candidate
            if decision is None:
                decision = await engine.authorize(
                    resolved_delete_paths,
                    context=context,
                    call_id=context.session_id,
                    tool_name=self.name,
                    arguments=arguments,
                    recursive=risk.recursive,
                    batch=risk.batch,
                    source="bash",
                )
            if not decision.allowed:
                raise ToolError(decision.reason)
        manifest = context.task_manifest
        tracked_paths: list[tuple[Path, bool, bool]] = []
        if isinstance(manifest, TaskFileManifest):
            for raw_path in [*risk.created_paths, *risk.generated_dirs]:
                candidate = Path(raw_path)
                if candidate.is_absolute() and candidate.parts[:2] == ("/", "workspace"):
                    candidate = root / Path(*candidate.parts[2:])
                elif not candidate.is_absolute():
                    candidate = cwd / candidate
                try:
                    candidate = safe_path(str(candidate), context)
                except ToolError:
                    continue
                tracked_paths.append(
                    (
                        candidate,
                        raw_path in risk.generated_dirs,
                        candidate.exists(),
                    )
                )
        configured_spec = context.sandbox_spec
        spec = configured_spec if isinstance(configured_spec, SandboxSpec) else None
        launch = self.sandbox_runner.build(
            command,
            workspace=root,
            cwd=cwd,
            writable=context.permission_mode == "workspace-write",
            network_mode=context.network_mode,
            setup_scope_approved=bool(context.metadata.get("network_scope_approved", False)),
            network_broker=(
                str(context.metadata["network_broker"])
                if context.metadata.get("network_broker")
                else None
            ),
            network_scope_approved=bool(context.metadata.get("network_scope_approved", False)),
            spec=spec,
        )
        # Popen 创建本身很快，stdout/stderr 改写入临时文件后，下面用异步轮询等待，既不
        # 阻塞事件循环，也不留下无主 Task。SandboxRunner 已经清理环境变量并组装 bwrap。
        timeout = self.timeout_seconds if self.timeout_seconds is not None else 60.0
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            try:
                # 先创建一个独立进程组，后续 terminate 才能连同命令派生的子进程一起回收。
                process = subprocess.Popen(
                    launch.argv,
                    env=launch.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ToolError(f"cannot start bash: {exc}") from exc
            try:
                await self._wait_process(process, timeout, context)
            except asyncio.TimeoutError as exc:
                await self._terminate_process(process)
                raise ToolError(f"bash timed out after {timeout} seconds") from exc
            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            # 只有进程已经被 poll 观察到结束后才读取输出，临时文件此时不会再增长。
            returncode = process.returncode or 0
        if isinstance(manifest, TaskFileManifest) and returncode == 0:
            for path, is_directory, existed_before in tracked_paths:
                if existed_before or not path.exists():
                    continue
                if is_directory and path.is_dir():
                    manifest.record_generated_dir(path)
                elif not is_directory and path.is_file():
                    manifest.record_created(path)
        return {
            "command": command,
            "cwd": str(cwd.relative_to(workspace_root(context))),
            "sandbox": launch.mode,
            "network_mode": launch.network_mode,
            "returncode": returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    @staticmethod
    async def _wait_process(
        process: subprocess.Popen[bytes],
        timeout: float,
        context: ToolContext,
    ) -> None:
        """异步轮询 Popen 状态，同时响应工具取消信号和超时。"""

        deadline = asyncio.get_running_loop().time() + timeout
        while process.poll() is None:
            if context.cancel_event.is_set():
                raise asyncio.CancelledError
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError
            await asyncio.sleep(0.05)

    @staticmethod
    async def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        """终止整个 Bash 进程组，超时后升级为 SIGKILL。"""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, OSError):
            pass
        deadline = asyncio.get_running_loop().time() + 2.0
        while process.poll() is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, OSError):
                pass
            process.poll()
