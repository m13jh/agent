"""内置工具共用的 workspace 路径校验逻辑。"""

from __future__ import annotations

from pathlib import Path

from python_agent.errors import ToolError
from python_agent.tools.types import ToolContext


def workspace_root(context: ToolContext) -> Path:
    """解析本次工具调用的 workspace 根目录；未指定时使用进程当前目录。"""

    return (context.workspace or Path.cwd()).resolve()


def safe_path(value: str, context: ToolContext) -> Path:
    """解析路径并验证它位于 workspace 内。

    ``resolve`` 同时展开 ``..`` 和符号链接，因此不能通过相对路径或指向外部的符号
    链接绕过边界；失败时抛出 ToolError，由 Pre 策略转换为模型可见错误。
    """

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
