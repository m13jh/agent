"""由受信任代码管理的 Docker L3 执行后端。

模型只能提供容器内 command 和 workspace-relative cwd。镜像、资源限制、网络模式、挂载
和安全选项都由 ContainerManager 固定生成，调用方不会获得 Docker socket 或 host mount
的参数注入机会。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from python_agent.errors import EnvironmentBlocked, ToolError
from python_agent.tools.capabilities import (
    CONTAINER_ADMIN,
    CONTAINER_EXECUTE,
    FILESYSTEM_WORKSPACE_WRITE,
    NetworkMode,
    NetworkModeName,
    coerce_network_mode,
)
from python_agent.tools.command_risk import CommandRiskAnalyzer
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.delete_policy import DeleteDecision, DeletePolicyEngine
from python_agent.tools.types import ToolContext


@dataclass(frozen=True, slots=True)
class ContainerLaunch:
    """已经由 ContainerManager 生成的不可变 Docker argv。"""

    argv: tuple[str, ...]
    environment: dict[str, str]
    image: str
    network_mode: str


class ContainerManager:
    """固定 Docker 安全参数的可信容器管理器。"""

    def __init__(
        self,
        *,
        docker_executable: str = "docker",
        image: str = "agent-runtime:latest",
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 256,
        timeout_seconds: float = 600.0,
    ) -> None:
        """初始化固定执行 Profile；不接受来自模型的 Docker 参数。"""

        if not docker_executable or any(character.isspace() for character in docker_executable):
            raise ValueError("docker executable must be one executable name/path")
        if not image or any(character.isspace() for character in image) or image.startswith("-"):
            raise ValueError("container image must be a single safe image reference")
        if pids_limit <= 0 or timeout_seconds <= 0:
            raise ValueError("container limits must be positive")
        self.docker_executable = docker_executable
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _paths(workspace: Path, cwd: Path | None) -> tuple[Path, Path]:
        root = workspace.expanduser().resolve()
        raw_cwd = cwd or workspace
        working = (
            (workspace / raw_cwd if not raw_cwd.is_absolute() else raw_cwd).expanduser().resolve()
        )
        if root == Path("/"):
            raise EnvironmentBlocked(
                "environment_blocked: container workspace cannot be filesystem root"
            )
        try:
            working.relative_to(root)
        except ValueError as exc:
            raise ToolError("container cwd is outside the workspace") from exc
        return root, working

    def build(
        self,
        command: str,
        *,
        workspace: Path,
        cwd: Path | None = None,
        writable: bool = True,
        network_mode: NetworkModeName | NetworkMode = "disabled",
        setup_scope_approved: bool = False,
        network_scope_approved: bool = False,
    ) -> ContainerLaunch:
        """构造没有 host root/socket/privileged 参数的 Docker 命令。"""

        if not command.strip():
            raise ToolError("container command cannot be empty")
        root, working = self._paths(workspace, cwd)
        mode = coerce_network_mode(network_mode)
        assert mode is not None
        if mode == NetworkMode.SETUP_APPROVED and not setup_scope_approved:
            raise EnvironmentBlocked(
                "environment_blocked: setup-approved network requires an approved setup scope"
            )
        if mode == NetworkMode.ALLOWLIST:
            raise EnvironmentBlocked(
                "environment_blocked: container allowlist mode requires a network broker"
            )
        if mode == NetworkMode.FULL and not network_scope_approved:
            raise EnvironmentBlocked(
                "environment_blocked: full container network requires an explicit approved scope"
            )

        docker = shutil.which(self.docker_executable) or self.docker_executable
        mount_mode = "readonly=false" if writable else "readonly=true"
        docker_network = "none" if mode == NetworkMode.DISABLED else "bridge"
        sandbox_cwd = Path("/workspace") / working.relative_to(root)
        argv: tuple[str, ...] = (
            docker,
            "run",
            "--rm",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            f"--pids-limit={self.pids_limit}",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            f"--network={docker_network}",
            "--mount",
            f"type=bind,src={root},dst=/workspace,{mount_mode}",
            "--workdir",
            str(sandbox_cwd),
            "--",
            self.image,
            "/bin/bash",
            "-lc",
            command,
        )
        argv_list = list(argv)
        for protected_name in (".python-agent", ".agent-trash"):
            insert_at = argv_list.index("--workdir")
            argv_list[insert_at:insert_at] = ["--tmpfs", f"/workspace/{protected_name}"]
        argv = tuple(argv_list)
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "TMPDIR": "/tmp",
            "DOCKER_CONFIG": "/tmp/python-agent-docker-config",
        }
        return ContainerLaunch(tuple(argv), environment, self.image, mode.value)

    async def run(
        self,
        command: str,
        *,
        workspace: Path,
        cwd: Path | None = None,
        writable: bool = True,
        network_mode: NetworkModeName | NetworkMode = "disabled",
        setup_scope_approved: bool = False,
        network_scope_approved: bool = False,
    ) -> dict[str, Any]:
        """启动容器并在取消/超时时回收整个 Docker 客户端进程组。"""

        root = workspace.expanduser().resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EnvironmentBlocked(
                f"environment_blocked: cannot initialize container workspace: {exc}"
            ) from exc
        launch = self.build(
            command,
            workspace=workspace,
            cwd=cwd,
            writable=writable,
            network_mode=network_mode,
            setup_scope_approved=setup_scope_approved,
            network_scope_approved=network_scope_approved,
        )
        root, working = self._paths(workspace, cwd)
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            try:
                process = subprocess.Popen(
                    launch.argv,
                    env=launch.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                raise EnvironmentBlocked(
                    f"environment_blocked: cannot start Docker: {exc}"
                ) from exc
            try:
                await self._wait(process)
            except asyncio.TimeoutError as exc:
                await self._terminate(process)
                raise ToolError(
                    f"container timed out after {self.timeout_seconds} seconds"
                ) from exc
            except asyncio.CancelledError:
                await self._terminate(process)
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
        return {
            "command": command,
            "cwd": str(working.relative_to(root)),
            "container": launch.image,
            "network_mode": launch.network_mode,
            "returncode": process.returncode or 0,
            "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
            "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
        }

    async def _wait(self, process: subprocess.Popen[bytes]) -> None:
        """轮询客户端进程，避免依赖 asyncio child watcher。"""

        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while process.poll() is None:
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError
            await asyncio.sleep(0.05)

    @staticmethod
    async def _terminate(process: subprocess.Popen[bytes]) -> None:
        """终止 Docker 客户端及其命令进程组。"""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
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
            except (OSError, ProcessLookupError):
                pass
            process.poll()


class ContainerExecTool:
    """将固定的 ContainerManager 作为 L3 工具暴露给模型。"""

    name = "container_exec"
    description = "Run a command inside the managed isolated Docker container."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "default": "."},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 600.0
    capabilities = ToolCapabilities(
        read_only=False,
        destructive=True,
        open_world=True,
        concurrency_safe=False,
        requires_approval=True,
        required_capabilities=frozenset(
            {CONTAINER_EXECUTE, CONTAINER_ADMIN, FILESYSTEM_WORKSPACE_WRITE}
        ),
    )

    def __init__(self, manager: ContainerManager | None = None) -> None:
        self.manager = manager or ContainerManager()

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        del arguments
        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """仅传递命令和安全 cwd；Docker Profile 不受模型参数控制。"""

        missing = set(self.capabilities.required_capabilities) - set(
            context.effective_capabilities()
        )
        if missing:
            raise ToolError(
                "permission denied: container executor is missing " + ", ".join(sorted(missing))
            )
        approved_tools = context.metadata.get("__approved_tool_names__", set())
        if not isinstance(approved_tools, set) or self.name not in approved_tools:
            # 直接绕过 ToolRuntime 时仍然 fail closed；正常 Gateway 会在 ApprovalPolicy
            # 成功后按 call id 设置标记，而不是让工具自己猜测用户意图。
            raise ToolError("approval denied: container execution must pass the Policy Gateway")
        approved_tools.discard(self.name)

        root = context.workspace or Path.cwd()
        raw_cwd = Path(arguments.get("cwd", "."))
        cwd = (
            root / Path(*raw_cwd.parts[2:])
            if raw_cwd.parts[:2] == ("/", "workspace")
            else root / raw_cwd
            if not raw_cwd.is_absolute()
            else raw_cwd
        )
        command = str(arguments["command"])
        risk = CommandRiskAnalyzer.analyze(command)
        if risk.reserved_path:
            raise ToolError("permission denied: agent infrastructure paths are not available")
        if risk.unknown_delete_scope:
            raise ToolError(
                "delete denied: container Shell deletion target scope is not fully determinable"
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
            resolved_paths = [
                (
                    str(root / Path(*Path(path).parts[2:]))
                    if Path(path).parts[:2] == ("/", "workspace")
                    else path
                    if Path(path).is_absolute()
                    else str(cwd / path)
                )
                for path in risk.delete_paths
            ]
            decision: DeleteDecision | None = None
            stored = context.metadata.get("__delete_decisions__", {})
            if len(resolved_paths) == 1 and isinstance(stored, dict):
                candidate = stored.get(str(Path(resolved_paths[0]).resolve()))
                if isinstance(candidate, DeleteDecision):
                    decision = candidate
            if decision is None:
                decision = await engine.authorize(
                    resolved_paths,
                    context=context,
                    call_id=context.session_id,
                    tool_name=self.name,
                    arguments=arguments,
                    recursive=risk.recursive,
                    batch=risk.batch,
                    source="container_exec",
                )
            if not decision.allowed:
                raise ToolError(decision.reason)
        return await self.manager.run(
            command,
            workspace=root,
            cwd=cwd,
            writable=context.permission_mode == "workspace-write",
            network_mode=context.network_mode or "disabled",
            setup_scope_approved=bool(context.metadata.get("network_scope_approved", False)),
            network_scope_approved=bool(context.metadata.get("network_scope_approved", False)),
        )


class DockerExecTool(ContainerExecTool):
    """兼容 ``docker_exec`` 工具名的显式别名。"""

    name = "docker_exec"


__all__ = [
    "ContainerExecTool",
    "ContainerLaunch",
    "ContainerManager",
    "DockerExecTool",
]
