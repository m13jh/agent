"""限制输出规模的 workspace 文本搜索工具。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._file_transaction import FileTransaction
from python_agent.tools.builtins._paths import safe_path, should_hide_path, workspace_root
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.types import ToolContext


class SearchTextTool:
    """在 workspace 内搜索文本的只读工具。

    优先使用系统的 rg 以获得更快搜索；如果 rg 不存在，则回退到 Python 遍历，保证
    基础功能在最小环境中仍可运行。
    """

    name = "search_text"
    description = "Search text files inside the workspace using ripgrep when available."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 20.0
    capabilities = ToolCapabilities(
        read_only=True,
        destructive=False,
        open_world=False,
        concurrency_safe=True,
        requires_approval=False,
    )

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """搜索只读取文件和进程输出，不改变 workspace 状态。"""

        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """校验搜索路径后选择 rg 子进程或 Python 回退实现。"""

        query = arguments["query"]
        FileTransaction.recover_pending(context.workspace or Path.cwd())
        root = safe_path(arguments.get("path", "."), context)
        maximum = min(arguments.get("max_results", 100), 200)
        if shutil.which("rg"):
            return await self._ripgrep(query, root, workspace_root(context), maximum, context)
        return await asyncio.to_thread(
            self._python_search,
            query,
            root,
            maximum,
            context,
        )

    async def _ripgrep(
        self,
        query: str,
        root: Path,
        workspace: Path,
        maximum: int,
        context: ToolContext,
    ) -> str:
        """以 argv 形式执行 rg，过滤凭据/基础设施，并在返回时实施全局上限。"""

        arguments = [
            "rg",
            "--line-number",
            "--with-filename",
            "--no-heading",
            "--no-follow",
            "--color",
            "never",
            "--max-count",
            str(maximum),
            "--glob",
            "!.env",
            "--glob",
            "!.env.*",
            "--glob",
            "!*.pem",
            "--glob",
            "!*.key",
            "--glob",
            "!*.p12",
            "--glob",
            "!*.pfx",
            "--glob",
            "!**/.ssh/**",
            "--glob",
            "!**/.aws/**",
            "--glob",
            "!**/.agent-trash/**",
        ]
        for excluded in context.excluded_paths:
            try:
                relative = excluded.expanduser().resolve().relative_to(root)
            except ValueError:
                continue
            arguments.extend(["--glob", f"!{relative.as_posix()}/**"])
        arguments.extend(
            [
                "--",
                query,
                str(root),
            ]
        )
        # 与 BashTool 相同，不依赖 WSL/沙箱中偶发失效的 asyncio child watcher。
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            try:
                process = subprocess.Popen(
                    arguments,
                    cwd=workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                return f"cannot start search: {exc}"
            try:
                while process.poll() is None:
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise
            stdout_file.seek(0)
            stdout = stdout_file.read()
            returncode = process.returncode
        if returncode not in (0, 1):
            return f"search failed with exit code {returncode}"
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[:maximum])

    @staticmethod
    async def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        """取消搜索时终止整个 rg 进程组，并在短暂宽限后升级 SIGKILL。"""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, OSError):
            pass
        deadline = asyncio.get_running_loop().time() + 1.0
        while process.poll() is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, OSError):
                pass
            process.poll()

    @staticmethod
    def _python_search(
        query: str,
        root: Path,
        maximum: int,
        context: ToolContext,
    ) -> str:
        """没有 rg 时逐文件逐行搜索，并跳过常见的缓存和构建目录。"""

        results: list[str] = []
        for path in sorted(root.rglob("*")):
            ignored = {
                ".git",
                ".venv",
                "__pycache__",
                "node_modules",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".python-agent",
                ".agent-trash",
                "dist",
                "build",
            }
            relative_parts = path.relative_to(root).parts
            if (
                not path.is_file()
                or any(part in ignored for part in relative_parts)
                or should_hide_path(path, context)
            ):
                continue
            try:
                safe_path(str(path), context)
            except (ToolError, ValueError):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    results.append(f"{path}:{number}:{line}")
                    if len(results) >= maximum:
                        return "\n".join(results)
        return "\n".join(results)
