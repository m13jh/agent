"""限制在 workspace 内列出文件的 list_files 工具。"""

from __future__ import annotations

from typing import Any

from python_agent.tools.builtins._paths import safe_path
from python_agent.tools.types import ToolContext


class ListFilesTool:
    """递归列出 workspace 内文件的只读工具。"""

    name = "list_files"
    description = "List files below a workspace directory."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory",
                "default": ".",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 10.0
    _ignored = {
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
        ".python-agent",
    }

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """目录遍历只读文件系统，不修改共享状态，可以被声明为并发安全。"""

        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> list[str]:
        """解析目录、过滤构建缓存并返回最多 max_results 个相对路径。"""

        root = safe_path(arguments.get("path", "."), context)
        maximum = min(arguments.get("max_results", 100), 500)
        paths: list[str] = []
        if not root.is_dir():
            return paths
        for path in sorted(root.rglob("*")):
            if any(part in self._ignored for part in path.parts):
                continue
            if path.is_file():
                paths.append(path.relative_to(root).as_posix())
                if len(paths) >= maximum:
                    break
        return paths
