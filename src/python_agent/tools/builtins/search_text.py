"""限制输出规模的 workspace 文本搜索工具。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from python_agent.tools.builtins._paths import safe_path, workspace_root
from python_agent.tools.types import ToolContext


class SearchTextTool:
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
        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
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
