"""为 Shell 工具提供尽力而为的操作系统隔离。

只要环境提供 bubblewrap，Shell 就会在其中启动。命名空间中只挂载 workspace 这个应用目录
和受信任 runtime 的只读目录；启动 ``bash`` 所需的系统目录以只读方式挂载，``/tmp`` 使用
独立的 tmpfs。显式
``network=disabled`` 且主机不允许创建网络命名空间时直接阻塞；只有旧版低层 API 未提供
网络 Profile 时才保留保守的 Shell 命令回退。找不到 bubblewrap 时采用 fail-closed，不会
静默退回宿主机 Shell。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from python_agent.errors import EnvironmentBlocked, SandboxError, ToolError
from python_agent.tools.capabilities import NetworkMode, NetworkModeName, coerce_network_mode
from python_agent.tools.runtime_env import RuntimeEnvironment


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """一次执行使用的不可变 Sandbox Profile。"""

    workspace: Path
    cwd: Path | None = None
    writable: bool = False
    network_mode: NetworkModeName = "disabled"
    setup_scope_approved: bool = False
    network_broker: str | None = None
    network_scope_approved: bool = False
    readonly_mounts: tuple[tuple[Path, str], ...] = ()
    network: NetworkModeName | None = None
    command: str = ""


@dataclass(frozen=True, slots=True)
class SandboxLaunch:
    """已经组装好的沙箱命令和最小子进程环境。"""

    argv: tuple[str, ...]
    environment: dict[str, str]
    mode: str
    network_mode: str = "disabled"

    @property
    def network(self) -> str:
        """``network_mode`` 的简短兼容别名。"""

        return self.network_mode


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
        command: str | SandboxSpec = "",
        *,
        workspace: Path | None = None,
        cwd: Path | None = None,
        writable: bool | None = None,
        network_mode: NetworkModeName | NetworkMode | None = None,
        network: NetworkModeName | NetworkMode | None = None,
        setup_scope_approved: bool = False,
        network_broker: str | None = None,
        network_scope_approved: bool = False,
        readonly_mounts: tuple[tuple[Path, str], ...] = (),
        spec: SandboxSpec | None = None,
    ) -> SandboxLaunch:
        """返回以 ``/workspace`` 为根目录的 bubblewrap 启动命令。

        显式 ``network_mode=disabled`` 时，无法创建 network namespace 会抛出
        ``EnvironmentBlocked``，绝不退回宿主网络。只有没有提供新 Profile 的旧低层调用
        才保留历史的本地命令白名单回退，以免破坏已有直接 BashTool 测试和扩展。
        """

        if isinstance(command, SandboxSpec):
            if spec is not None:
                raise ValueError("provide SandboxSpec either as command or spec, not both")
            spec = command
            command = spec.command
        if spec is not None:
            workspace = spec.workspace
            cwd = spec.cwd or spec.workspace
            writable = spec.writable
            network_mode = spec.network_mode
            setup_scope_approved = spec.setup_scope_approved
            network_scope_approved = spec.network_scope_approved
            readonly_mounts = spec.readonly_mounts
            if spec.network is not None:
                network_mode = spec.network
        if network_mode is None and network is not None:
            network_mode = network
        if workspace is None or cwd is None or writable is None:
            raise ValueError("workspace, cwd and writable are required for SandboxRunner.build")
        if not command.strip():
            raise ToolError("Bash command cannot be empty")
        legacy_network_mode = network_mode is None
        resolved_network = coerce_network_mode(network_mode)
        if resolved_network is None:
            # 低层旧 API 的行为仅用于兼容；AgentLoop 总会传入 config.network_mode。
            network_name = "legacy"
        else:
            network_name = resolved_network.value
            if resolved_network == NetworkMode.SETUP_APPROVED and not setup_scope_approved:
                raise EnvironmentBlocked(
                    "environment_blocked: setup-approved network requires an approved setup scope"
                )
            if resolved_network == NetworkMode.ALLOWLIST:
                raise EnvironmentBlocked(
                    "environment_blocked: allowlist network requires a broker-backed sandbox runner"
                )
            if resolved_network == NetworkMode.FULL and not network_scope_approved:
                raise EnvironmentBlocked(
                    "environment_blocked: full network requires an explicit approved scope"
                )

        if os.name != "posix":
            raise EnvironmentBlocked(
                "environment_blocked: Bash sandbox is unavailable on this platform; "
                "refusing host shell"
            )
        bubblewrap = shutil.which("bwrap") or shutil.which("bubblewrap")
        if bubblewrap is None:
            raise EnvironmentBlocked(
                "environment_blocked: Bash sandbox requires bubblewrap (bwrap); "
                "refusing to run an unsandboxed shell"
            )

        root = workspace.expanduser().resolve()
        working = (workspace / cwd if not cwd.is_absolute() else cwd).expanduser().resolve()
        if root == Path("/"):
            raise EnvironmentBlocked(
                "environment_blocked: Bash workspace cannot be the filesystem root"
            )
        try:
            relative_cwd = working.relative_to(root)
        except ValueError as exc:
            raise ToolError("Bash cwd is outside the workspace") from exc

        network_isolated = self._network_namespace_supported(
            bubblewrap,
            self.probe_timeout_seconds,
        )
        if resolved_network == NetworkMode.DISABLED and not network_isolated:
            raise EnvironmentBlocked(
                "environment_blocked: network namespace is unavailable; refusing "
                "network-enabled host shell"
            )
        if not network_isolated and legacy_network_mode:
            self._validate_restricted_command(command)

        arguments: list[str] = [bubblewrap, "--die-with-parent", "--new-session"]
        isolate_network = resolved_network == NetworkMode.DISABLED or legacy_network_mode
        if isolate_network and network_isolated:
            arguments.append("--unshare-net")
        # 在不暴露宿主机文件系统的前提下保持基本运行能力。bubblewrap 中未绑定的目录在
        # 命名空间内不可见，workspace 会单独挂载；常见顶层目录只创建为空壳，方便命令
        # 看到标准 Linux 层次结构，但不会因此看到宿主目录内容。
        for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32"):
            if Path(directory).exists():
                arguments.extend(["--ro-bind", directory, directory])
        virtual_directories = (
            "/home",
            "/root",
            "/opt",
            "/var",
            "/mnt",
            "/run",
            "/media",
            "/srv",
            "/etc",
        )
        created_directories = {Path(directory) for directory in virtual_directories}
        for virtual_directory in virtual_directories:
            arguments.extend(["--dir", virtual_directory])
        runtime = RuntimeEnvironment.discover()
        runtime_parent_dirs: set[Path] = set()
        for mount in runtime.mounts:
            current = mount.parent
            while current != Path("/"):
                runtime_parent_dirs.add(current)
                current = current.parent
        for parent_directory in sorted(
            runtime_parent_dirs,
            key=lambda value: (len(value.parts), str(value)),
        ):
            if parent_directory not in created_directories:
                arguments.extend(["--dir", str(parent_directory)])
                created_directories.add(parent_directory)
        for mount in runtime.mounts:
            arguments.extend(["--ro-bind", str(mount), str(mount)])
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
        # 事务、Session、manifest 和 soft-delete trash 属于宿主侧安全基础设施；即使
        # Shell 通过间接路径构造字符串，也只能看到隔离的空 tmpfs，不能篡改真实状态。
        for protected_name in (".python-agent", ".agent-trash"):
            # 只有宿主目录已经存在时才覆盖为 tmpfs。不存在的目录不能在只读 workspace
            # bind mount 内使用 --dir/--tmpfs 创建；它本身也不需要暴露，且 CommandRisk
            # 会拒绝命令中显式访问这些保留名称。
            if (root / protected_name).is_dir():
                arguments.extend(["--tmpfs", f"/workspace/{protected_name}"])
        # 网络开启时只读挂载解析和 TLS 所需的最小系统文件；不把宿主整个 /etc 暴露给
        # Agent，网络关闭时挂载这些文件也不会赋予任何网络能力。
        for system_file in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            if Path(system_file).is_file():
                arguments.extend(["--ro-bind", system_file, system_file])
        certificates = Path("/etc/ssl/certs")
        if certificates.is_dir():
            arguments.extend(["--dir", "/etc/ssl"])
            arguments.extend(["--ro-bind", str(certificates), "/etc/ssl/certs"])
        for source, target in readonly_mounts:
            source_path = source.expanduser().resolve()
            if source_path == Path("/") or not source_path.exists():
                raise EnvironmentBlocked(
                    f"environment_blocked: invalid read-only mount source {source}"
                )
            target_path = Path(target)
            if not target_path.is_absolute() or target_path == Path("/"):
                raise EnvironmentBlocked(
                    f"environment_blocked: invalid read-only mount target {target}"
                )
            arguments.extend(["--ro-bind", str(source_path), str(target_path)])
        sandbox_cwd = Path("/workspace") / relative_cwd
        arguments.extend(["--chdir", str(sandbox_cwd), "--", "bash", "-lc", command])

        workspace_path_entries: list[str] = []
        for environment_name in (".venv", "venv"):
            environment_root = root / environment_name
            try:
                environment_root.resolve().relative_to(root)
            except ValueError:
                continue
            if (environment_root / "bin").is_dir():
                workspace_path_entries.append(f"/workspace/{environment_name}/bin")
        environment: dict[str, str] = {
            "PATH": ":".join(
                workspace_path_entries
                + [str(path) for path in runtime.path_entries]
                + ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
            ),
            "HOME": "/tmp/home",
            "TMPDIR": "/tmp",
        }
        environment.update(dict(runtime.environment))
        for key in ("LANG", "LC_ALL", "LC_CTYPE", "TERM"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        if resolved_network == NetworkMode.DISABLED and network_isolated:
            mode = "bubblewrap-network"
        elif legacy_network_mode:
            mode = "bubblewrap-network" if network_isolated else "bubblewrap-filesystem-restricted"
        else:
            mode = "bubblewrap-network-enabled"
        return SandboxLaunch(tuple(arguments), environment, mode, network_name)

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


__all__ = [
    "EnvironmentBlocked",
    "SandboxError",
    "SandboxLaunch",
    "SandboxRunner",
    "SandboxSpec",
]
