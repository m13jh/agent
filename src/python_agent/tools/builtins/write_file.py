"""限制在 workspace 内写入 UTF-8 文本的 write_file 工具。"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._file_transaction import FileTransaction
from python_agent.tools.builtins._paths import safe_path, workspace_root
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.types import ToolContext


class WriteFileTool:
    """使用临时文件加替换的方式写入文本，减少写入中断造成半文件的概率。"""

    name = "write_file"
    description = "Write UTF-8 text to a file inside the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "content": {"type": "string", "description": "Complete file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 10.0
    capabilities = ToolCapabilities(
        read_only=False,
        destructive=True,
        open_world=False,
        concurrency_safe=False,
        requires_approval=False,
    )

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """写文件会改变共享 workspace，必须视为 exclusive 工具。"""

        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """以 UTF-8 原子替换方式写文件。

        Runtime 的 Pre 策略已经检查权限和路径；这里再次检查 permission_mode，防止
        业务工具被绕过 Runtime 直接调用时意外写入只读 workspace。
        """

        if context.permission_mode != "workspace-write":
            raise ToolError("write_file requires workspace-write mode")
        FileTransaction.recover_pending(workspace_root(context))
        path = safe_path(arguments["path"], context)
        if path.exists() and path.is_dir():
            raise ToolError(f"cannot write directory: {arguments['path']}")
        content = arguments["content"]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        except (OSError, UnicodeError) as exc:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise ToolError(f"cannot write {arguments['path']}: {exc}") from exc
        return {
            "path": path.relative_to(workspace_root(context)).as_posix(),
            "bytes_written": len(content.encode("utf-8")),
        }
