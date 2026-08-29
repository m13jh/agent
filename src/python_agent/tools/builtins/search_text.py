"""限制输出规模的 workspace 文本搜索工具。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from python_agent.tools.builtins._paths import safe_path, workspace_root
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

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """搜索只读取文件和进程输出，不改变 workspace 状态。"""

        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """校验搜索路径后选择 rg 子进程或 Python 回退实现。"""

        query = arguments["query"]
        root = safe_path(arguments.get("path", "."), context)
        maximum = min(arguments.get("max_results", 100), 200)
        if shutil.which("rg"):
            return await self._ripgrep(query, root, workspace_root(context), maximum)
        return await asyncio.to_thread(
            self._python_search,
            query,
            root,
            workspace_root(context),
            maximum,
        )

    async def _ripgrep(self, query: str, root: Path, workspace: Path, maximum: int) -> str:
        """以 argv 形式执行 rg，避免通过 Shell 拼接 query 造成命令注入。"""

        process = await asyncio.create_subprocess_exec(
            "rg",
            "--line-number",
            "--with-filename",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(maximum),
            "--",
            query,
            str(root),
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode not in (0, 1):
            return f"search failed with exit code {process.returncode}"
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _python_search(query: str, root: Path, workspace: Path, maximum: int) -> str:
        """没有 rg 时逐文件逐行搜索，并跳过常见的缓存和构建目录。"""

        del workspace
        results: list[str] = []
        for path in sorted(root.rglob("*")):
            ignored = {".git", ".venv", "__pycache__", "node_modules"}
            if not path.is_file() or any(part in ignored for part in path.parts):
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
