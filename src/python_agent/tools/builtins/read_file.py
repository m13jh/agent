"""限制在 workspace 内读取 UTF-8 文本文件的 read_file 工具。"""

from __future__ import annotations

from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._paths import safe_path
from python_agent.tools.types import ToolContext


class ReadFileTool:
    """按行读取 workspace 内 UTF-8 文件的只读工具。"""

    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace by line range."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "offset": {"type": "integer", "minimum": 0, "description": "Zero-based line offset"},
            "limit": {"type": "integer", "minimum": 1, "description": "Maximum lines to return"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 10.0

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """文件读取不会修改文件，因此可与其他只读调用并发。"""

        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """读取文件并应用 offset/limit 行窗口，避免一次把整个大文件送入模型。"""

        path = safe_path(arguments["path"], context)
        if not path.is_file():
            raise ToolError(f"file does not exist: {arguments['path']}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolError(f"cannot read {arguments['path']}: {exc}") from exc
        lines = text.splitlines(keepends=True)
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit")
        selected = lines[offset:] if limit is None else lines[offset : offset + limit]
        return "".join(selected)
