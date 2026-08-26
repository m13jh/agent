"""内置工具共用的 workspace 路径校验逻辑。"""

from __future__ import annotations

from pathlib import Path

from python_agent.errors import ToolError
from python_agent.tools.types import ToolContext


def workspace_root(context: ToolContext) -> Path:
    return (context.workspace or Path.cwd()).resolve()


def safe_path(value: str, context: ToolContext) -> Path:
    root = workspace_root(context)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolError(f"path is outside workspace: {value}") from exc
    return resolved
