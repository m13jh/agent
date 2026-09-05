"""内置工具共用的 workspace 路径校验逻辑。"""

from __future__ import annotations

from pathlib import Path

from python_agent.errors import ToolError
from python_agent.tools.types import ToolContext

_SENSITIVE_EXACT_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_DIRECTORIES = {".ssh", ".aws", ".gnupg", ".azure", "gcloud"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
_RESERVED_DIRECTORIES = {".python-agent", ".agent-trash"}


def workspace_root(context: ToolContext) -> Path:
    """解析本次工具调用的 workspace 根目录；未指定时使用进程当前目录。"""

    root = (context.workspace or Path.cwd()).resolve()
    if root == Path("/"):
        raise ToolError("workspace root cannot be the filesystem root")
    return root


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
    if is_excluded_path(resolved, context):
        raise ToolError(f"path is reserved for agent infrastructure: {value}")
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        relative = resolved
    if relative.parts and relative.parts[0] in _RESERVED_DIRECTORIES:
        raise ToolError(f"path is reserved for agent infrastructure: {value}")
    if is_sensitive_path(resolved, context):
        raise ToolError(f"access to sensitive credential path is denied: {value}")
    return resolved


def is_excluded_path(path: Path, context: ToolContext) -> bool:
    """判断目标是否等于或位于调用方声明的基础设施目录内。"""

    resolved = path.resolve()
    for excluded in context.excluded_paths:
        root = excluded.expanduser().resolve()
        if resolved == root:
            return True
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def is_sensitive_path(path: Path, context: ToolContext) -> bool:
    """按 workspace 相对路径识别常见凭据文件和密钥目录。"""

    resolved = path.resolve()
    root = workspace_root(context)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        relative = resolved
    names = [part.lower() for part in relative.parts]
    if any(name in _SENSITIVE_DIRECTORIES for name in names):
        return True
    name = resolved.name.lower()
    if name in _SAFE_ENV_EXAMPLES:
        return False
    if name in _SENSITIVE_EXACT_NAMES or name.startswith(".env."):
        return True
    return resolved.suffix.lower() in _SENSITIVE_SUFFIXES


def should_hide_path(path: Path, context: ToolContext) -> bool:
    """目录遍历和搜索共用的过滤判断。"""

    return is_excluded_path(path, context) or is_sensitive_path(path, context)
