"""为 Shell 工具提供尽力而为的操作系统隔离。

只要环境提供 bubblewrap，Shell 就会在其中启动。命名空间中只挂载 workspace 这个应用目录；
启动 ``bash`` 所需的系统目录以只读方式挂载，``/tmp`` 使用独立的 tmpfs。如果主机不允许
创建网络命名空间（例如部分 WSL/容器配置），除了文件系统隔离外还会启用保守的 Shell
命令策略。找不到 bubblewrap 时采用 fail-closed，不会静默退回宿主机 Shell。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from python_agent.errors import ToolError


@dataclass(frozen=True, slots=True)
class SandboxLaunch:
    """已经组装好的沙箱命令和最小子进程环境。"""

    argv: tuple[str, ...]
    environment: dict[str, str]
    mode: str


class SandboxRunner:
    """为 workspace Shell 构建 fail-closed 的 bubblewrap 启动命令。"""

    _safe_commands = frozenset(
        {
            "basename",
            "cat",
            "cd",
            "cmp",
            "cp",
            "cut",
            "diff",
            "dirname",
            "echo",
            "false",
            "find",
            "grep",
            "head",
            "ls",
            "mkdir",
            "mv",
            "printf",
            "pwd",
            "readlink",
            "rm",
            "rmdir",
            "sleep",
            "sort",
            "tail",
            "tee",
            "test",
            "touch",
            "tr",
            "true",
            "uniq",
            "wait",
            "wc",
            "which",
        }
    )
    _operators = frozenset({";", "&", "&&", "|", "||"})
    _redirections = frozenset({"<", ">", "<<", ">>", "<&", ">&", "<>"})
    _dangerous_tokens = frozenset(
        {
            "-exec",
            "-execdir",
            "--exec",
            "xargs",
            "command",
            "builtin",
            "eval",
            "exec",
            "source",
            ".",
            "export",
            "unset",
            "set",
            "declare",
            "local",
            "readonly",
            "trap",
            "ulimit",
            "umask",
        }
    )

    def __init__(self, *, probe_timeout_seconds: float = 2.0) -> None:
        if probe_timeout_seconds <= 0:
            raise ValueError("probe_timeout_seconds must be positive")
        self.probe_timeout_seconds = probe_timeout_seconds

    def build(
        self,
        command: str,
        *,
        workspace: Path,
        cwd: Path,
        writable: bool,
    ) -> SandboxLaunch:
        """返回以 ``/workspace`` 为根目录的 bubblewrap 启动命令。"""

        if os.name != "posix":
            raise ToolError("Bash sandbox is unavailable on this platform; refusing host shell")
        bubblewrap = shutil.which("bwrap") or shutil.which("bubblewrap")
        if bubblewrap is None:
            raise ToolError(
                "Bash sandbox requires bubblewrap (bwrap); refusing to run an unsandboxed shell"
            )

        root = workspace.expanduser().resolve()
        working = cwd.expanduser().resolve()
        if root == Path("/"):
            raise ToolError("Bash workspace cannot be the filesystem root")
        try:
            relative_cwd = working.relative_to(root)
        except ValueError as exc:
            raise ToolError("Bash cwd is outside the workspace") from exc

        network_isolated = self._network_namespace_supported(
            bubblewrap,
            self.probe_timeout_seconds,
        )
        if not network_isolated:
            self._validate_restricted_command(command)

        arguments: list[str] = [bubblewrap, "--die-with-parent", "--new-session"]
        if network_isolated:
            arguments.append("--unshare-net")
        # 在不暴露宿主机文件系统的前提下保持基本运行能力。bubblewrap 中未绑定的目录在
        # 命名空间内不可见，workspace 会单独挂载。
        for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32"):
            if Path(directory).exists():
                arguments.extend(["--ro-bind", directory, directory])
        arguments.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/home",
                "--bind" if writable else "--ro-bind",
                str(root),
                "/workspace",
            ]
        )
        sandbox_cwd = Path("/workspace") / relative_cwd
        arguments.extend(["--chdir", str(sandbox_cwd), "--", "bash", "-lc", command])

        environment: dict[str, str] = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/tmp/home",
            "TMPDIR": "/tmp",
        }
        for key in ("LANG", "LC_ALL", "LC_CTYPE", "TERM"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        mode = "bubblewrap-network" if network_isolated else "bubblewrap-filesystem-restricted"
        return SandboxLaunch(tuple(arguments), environment, mode)

    @staticmethod
    @lru_cache(maxsize=8)
    def _network_namespace_supported(bubblewrap: str, timeout: float) -> bool:
        """每个可执行文件只探测一次；不支持的 WSL/容器主机会返回 False。"""

        try:
            result = subprocess.run(
                [
                    bubblewrap,
                    "--die-with-parent",
                    "--new-session",
                    # 探测命令本身必须挂载要执行的程序。缺少这个绑定时，即使网络命名空间
                    # 完全可用，bwrap 也可能因为 ``execvp /bin/true: No such file`` 而失败。
                    "--ro-bind",
                    "/",
                    "/",
                    "--unshare-net",
                    "--",
                    "/bin/true",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _validate_restricted_command(self, command: str) -> None:
        """在没有网络隔离时，拒绝可能重新获得开放访问能力的 Shell 结构。"""

        if not command.strip():
            raise ToolError("Bash command cannot be empty")
        if any(marker in command for marker in ("$({", "$(", "`", "<(", ">(", "${", "\n", "\r")):
            raise ToolError(
                "Bash sandbox cannot isolate this shell construct on the current host; "
                "remove command substitution, process substitution or multiline input"
            )
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError as exc:
            raise ToolError(f"Bash command has invalid shell syntax: {exc}") from exc

        next_command = True
        for token in tokens:
            if token in self._operators:
                next_command = True
                continue
            if token in self._redirections:
                continue
            if token in self._dangerous_tokens:
                raise ToolError(f"Bash sandbox rejects shell token: {token}")
            if token in {"2", "1", "0"}:
                # 常见的文件描述符重定向不是命令名（例如 ``2>&1``）。
                continue
            if next_command:
                if "/" in token:
                    raise ToolError(
                        "Bash sandbox rejects explicit executable paths when network "
                        "isolation is unavailable"
                    )
                command_name = Path(token).name
                if command_name not in self._safe_commands:
                    raise ToolError(
                        f"Bash sandbox cannot prove command is local and safe: {command_name}"
                    )
                next_command = False

            if token.startswith("/"):
                normalized = Path(token)
                workspace_path = Path("/workspace")
                if not (normalized == workspace_path or workspace_path in normalized.parents):
                    raise ToolError(
                        "Bash sandbox rejects absolute paths outside /workspace when network "
                        "isolation is unavailable"
                    )
            if ".." in Path(token).parts:
                raise ToolError("Bash sandbox rejects parent-directory traversal")


__all__ = ["SandboxLaunch", "SandboxRunner"]
