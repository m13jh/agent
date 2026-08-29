"""受审批、限时并可回收进程组的 Bash 工具。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile
from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._paths import safe_path, workspace_root
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
        cwd = safe_path(arguments.get("cwd", "."), context)
        if not cwd.is_dir():
            raise ToolError(f"bash cwd is not a directory: {arguments.get('cwd', '.')}")
        environment = os.environ.copy()
        # 子进程不需要读取模型凭据，避免命令环境意外暴露 API Key。
        for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            environment.pop(key, None)
        # 不使用 asyncio.create_subprocess_exec：在部分 WSL/沙箱环境中重复启动异步子进程
        # 会出现 child watcher 无法收到退出通知的问题。Popen 创建本身很快，stdout/stderr
        # 改写入临时文件后，下面用异步轮询等待，既不阻塞事件循环，也不留下无主 Task。
        timeout = self.timeout_seconds if self.timeout_seconds is not None else 60.0
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            try:
                # 先创建一个独立进程组，后续 terminate 才能连同命令派生的子进程一起回收。
                process = subprocess.Popen(
                    ["bash", "-lc", command],
                    cwd=str(cwd),
                    env=environment,
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
        return {
            "command": command,
            "cwd": str(cwd.relative_to(workspace_root(context))),
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
