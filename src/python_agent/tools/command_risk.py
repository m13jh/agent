"""不执行 Shell 的命令风险分析器。

它只为 Policy Gateway 提供保守的分类，不承担 workspace 隔离；真正的文件边界仍由
路径策略和 SandboxRunner 强制。无法完整解析的删除范围不会被当成安全命令。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RiskKind = Literal["none", "delete", "batch-delete", "host-admin", "network"]


@dataclass(frozen=True, slots=True)
class CommandRisk:
    """一次 Shell 命令的可审计风险摘要。"""

    kind: RiskKind = "none"
    delete_paths: tuple[str, ...] = ()
    recursive: bool = False
    batch: bool = False
    unknown_delete_scope: bool = False
    network_required: bool = False
    host_admin: bool = False
    read_only: bool = True
    created_paths: tuple[str, ...] = ()
    generated_dirs: tuple[str, ...] = ()
    reserved_path: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def destructive(self) -> bool:
        """是否包含删除或其他会改写工作区的操作。"""

        return bool(
            not self.read_only or self.delete_paths or self.unknown_delete_scope or self.host_admin
        )

    @property
    def requires_approval(self) -> bool:
        """是否至少需要进入审批/策略拒绝流程。"""

        return self.destructive or self.network_required

    @property
    def is_delete(self) -> bool:
        """``delete_paths``/未知删除范围的兼容判断。"""

        return bool(self.delete_paths or self.unknown_delete_scope)

    @property
    def is_batch(self) -> bool:
        return self.batch or self.recursive


class CommandRiskAnalyzer:
    """识别 Shell 删除、网络和宿主机管理操作，无法证明安全时保持保守。"""

    _operators = frozenset({";", "&", "&&", "|", "||"})
    _redirections = frozenset({"<", ">", "<<", ">>", "<&", ">&", "<>"})
    _network_commands = frozenset(
        {
            "conda",
            "curl",
            "wget",
            "pip",
            "pip3",
            "npm",
            "pnpm",
            "yarn",
            "cargo",
            "go",
            "uv",
            "git",
        }
    )
    _host_commands = frozenset(
        {
            "sudo",
            "su",
            "apt",
            "apt-get",
            "dnf",
            "yum",
            "pacman",
            "systemctl",
            "service",
            "mount",
            "umount",
            "iptables",
            "firewall-cmd",
        }
    )
    _write_commands = frozenset(
        {
            "cp",
            "mv",
            "mkdir",
            "touch",
            "tee",
            "sed",
            "perl",
        }
    )
    _read_only_commands = frozenset(
        {
            "basename",
            "cat",
            "cmp",
            "cut",
            "diff",
            "dirname",
            "find",
            "grep",
            "head",
            "ls",
            "pwd",
            "readlink",
            "sort",
            "tail",
            "test",
            "tr",
            "uniq",
            "wc",
            "which",
        }
    )
    _version_only_flags = frozenset({"--version", "-V", "-v", "-version", "version"})
    _network_interpreter_markers = frozenset(
        {
            "http://",
            "https://",
            "urllib",
            "requests",
            "httpx",
            "socket",
            "fetch(",
            "axios",
        }
    )
    _version_commands = frozenset(
        {
            "cmake",
            "conda",
            "gcc",
            "g++",
            "java",
            "javac",
            "make",
            "node",
            "npm",
            "pip",
            "pip3",
            "pytest",
            "python",
            "python3",
        }
    )

    @classmethod
    def _segments(cls, command: str) -> list[list[str]]:
        """用 Shell 词法切分命令链，不执行任何内容。"""

        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
        segments: list[list[str]] = [[]]
        for token in tokens:
            if token in cls._operators:
                if segments[-1]:
                    segments.append([])
            else:
                segments[-1].append(token)
        return [segment for segment in segments if segment]

    @staticmethod
    def _basename(value: str) -> str:
        return Path(value).name.lower()

    @staticmethod
    def _has_glob(value: str) -> bool:
        return any(marker in value for marker in ("*", "?", "[", "]", "{", "}"))

    @staticmethod
    def _delete_operands(tokens: list[str]) -> tuple[list[str], bool]:
        """提取 rm/unlink/rmdir 操作数和是否带递归/批量标记。"""

        operands: list[str] = []
        recursive = False
        after_options = False
        redirections = {"<", ">", "<<", ">>", "<&", ">&", "<>"}
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in redirections:
                index += 2
                continue
            if token in {"0", "1", "2"} and index + 1 < len(tokens):
                if tokens[index + 1] in redirections:
                    index += 3
                    continue
            if token == "--":
                after_options = True
                index += 1
                continue
            if not after_options and token.startswith("-") and token != "-":
                normalized = token.lstrip("-")
                recursive = recursive or "r" in normalized or "R" in normalized
                index += 1
                continue
            operands.append(token)
            index += 1
        return operands, recursive

    @staticmethod
    def _path_operands(tokens: list[str]) -> list[str]:
        """提取 mkdir/touch 等简单命令的显式路径参数。"""

        operands: list[str] = []
        after_options = False
        for token in tokens[1:]:
            if token == "--":
                after_options = True
                continue
            if not after_options and token.startswith("-") and token != "-":
                continue
            operands.append(token)
        return operands

    @staticmethod
    def _is_safe_fd_dup(tokens: list[str], index: int) -> bool:
        """允许 ``2>&1`` 这类不写文件的标准流复制。"""

        if tokens[index] not in {"<&", ">&"}:
            return False
        return (
            index > 0
            and index + 1 < len(tokens)
            and tokens[index - 1] in {"0", "1", "2"}
            and tokens[index + 1] in {"0", "1", "2"}
        )

    @classmethod
    def _without_safe_fd_dup(cls, tokens: list[str]) -> list[str]:
        """去掉 ``2>&1`` 的三个词元，便于判断命令本身是否是版本查询。"""

        result: list[str] = []
        index = 0
        while index < len(tokens):
            if (
                index + 2 < len(tokens)
                and tokens[index] in {"0", "1", "2"}
                and tokens[index + 1] in {"<&", ">&"}
                and tokens[index + 2] in {"0", "1", "2"}
            ):
                index += 3
                continue
            result.append(tokens[index])
            index += 1
        return result

    @classmethod
    def _find_root(cls, tokens: list[str]) -> str:
        """取得 find 的起始目录；无法识别时返回 workspace 根的相对点。"""

        for token in tokens[1:]:
            if token in {"-H", "-L", "-P"} or token.startswith("-"):
                continue
            return token
        return "."

    @classmethod
    def analyze(cls, command: str) -> CommandRisk:
        """返回命令风险，不在任何情况下启动子进程。"""

        if not command or not command.strip():
            return CommandRisk(read_only=False, reasons=("empty command",))
        reasons: list[str] = []
        delete_paths: list[str] = []
        recursive = False
        batch = False
        unknown_delete_scope = False
        network_required = False
        host_admin = False
        read_only = True
        created_paths: list[str] = []
        generated_dirs: list[str] = []
        reserved_path = False

        try:
            segments = cls._segments(command)
        except ValueError:
            # 解析失败时不能证明命令没有删除或宿主机副作用；由上层拒绝开放能力命令。
            return CommandRisk(
                kind="host-admin",
                unknown_delete_scope=True,
                host_admin=True,
                read_only=False,
                reasons=("shell syntax could not be parsed",),
            )

        for tokens in segments:
            if not tokens:
                continue
            if any(
                reserved in token
                for token in tokens
                for reserved in (".python-agent", ".agent-trash")
            ):
                reserved_path = True
            while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
                tokens = tokens[1:]
            if not tokens:
                continue
            command_name = cls._basename(tokens[0])
            if command_name == "command" and len(tokens) == 3 and tokens[1] in {"-v", "-V"}:
                # ``command -v`` 只查询 PATH；保留它用于环境诊断，但不把通用 command
                # 包装器（例如 command rm）误当成安全命令。
                continue
            if command_name in {"eval", "source", ".", "command", "env", "xargs"}:
                # 这些命令可以动态替换/构造真正的执行体，普通 token 分析无法证明其
                # 删除范围；交给 Gateway 拒绝未知删除，而不是把它当成无副作用包装器。
                read_only = False
                unknown_delete_scope = True
                batch = True
                reasons.append(f"dynamic shell wrapper: {command_name}")
                continue
            if command_name == "busybox" and any(
                token in {"rm", "unlink", "rmdir"} for token in tokens[1:]
            ):
                read_only = False
                unknown_delete_scope = True
                batch = True
                reasons.append("busybox filesystem deletion")
                continue
            if command_name in {"bash", "sh", "dash", "zsh"}:
                # ``bash -c 'rm file'`` 会把真正的删除藏在一个已经被 shlex 合并的
                # 字符串中；递归分析该字符串，避免通过嵌套 Shell 绕过同一策略。
                read_only = False
                for index, token in enumerate(tokens[:-1]):
                    if token not in {"-c", "-lc", "-cl"}:
                        continue
                    nested = cls.analyze(tokens[index + 1])
                    delete_paths.extend(nested.delete_paths)
                    recursive = recursive or nested.recursive
                    batch = batch or nested.batch
                    unknown_delete_scope = unknown_delete_scope or nested.unknown_delete_scope
                    network_required = network_required or nested.network_required
                    host_admin = host_admin or nested.host_admin
                    read_only = read_only and nested.read_only
                    reasons.extend(nested.reasons)
                    reserved_path = reserved_path or nested.reserved_path
                continue
            if command_name in cls._host_commands:
                host_admin = True
                read_only = False
                reasons.append(f"host command: {command_name}")
                if command_name in {
                    "apt",
                    "apt-get",
                    "dnf",
                    "yum",
                    "pacman",
                } and any(
                    token in {"install", "update", "upgrade", "fetch", "download"}
                    for token in tokens[1:]
                ):
                    network_required = True
                # sudo apt/systemctl 的实际目标无法由普通 workspace 策略证明。
                if command_name in {"sudo", "su"} and len(tokens) > 1:
                    nested_command = cls._basename(tokens[1])
                    if nested_command in {"rm", "unlink", "rmdir"}:
                        unknown_delete_scope = True
                continue

            if command_name in cls._network_commands:
                version_only = bool(tokens[1:]) and all(
                    token in cls._version_only_flags for token in tokens[1:]
                )
                non_option_tokens = [token for token in tokens[1:] if not token.startswith("-")]
                if command_name in {"curl", "wget"}:
                    network_required = not version_only
                elif command_name in {"pip", "pip3", "npm", "pnpm", "yarn", "uv", "conda"}:
                    network_required = any(
                        token
                        in {
                            "add",
                            "audit",
                            "ci",
                            "create",
                            "download",
                            "fetch",
                            "get",
                            "index",
                            "install",
                            "outdated",
                            "publish",
                            "search",
                            "update",
                            "upgrade",
                            "view",
                            "wheel",
                        }
                        for token in non_option_tokens
                    )
                elif command_name == "cargo":
                    network_required = not version_only and any(
                        token in {"add", "build", "fetch", "install", "run", "update"}
                        for token in non_option_tokens
                    )
                elif command_name == "go":
                    network_required = not version_only and any(
                        token in {"build", "get", "install", "mod", "run", "test", "work"}
                        for token in non_option_tokens
                    )
                elif command_name == "git" and any(
                    token in {"clone", "fetch", "pull", "push", "submodule"} for token in tokens[1:]
                ):
                    network_required = True
                if network_required:
                    reasons.append(f"network-capable command: {command_name}")

            if command_name in {"python", "python3", "node", "ruby", "perl", "java"}:
                source = " ".join(tokens[1:]).lower()
                if any(marker in source for marker in cls._network_interpreter_markers) or any(
                    token in {"pip", "install", "download", "fetch"} for token in tokens[1:]
                ):
                    network_required = True
                    reasons.append(f"network-capable interpreter operation: {command_name}")

            if command_name in {"rm", "unlink", "rmdir"}:
                operands, operation_recursive = cls._delete_operands(tokens)
                read_only = False
                recursive = recursive or operation_recursive or command_name == "rmdir"
                if not operands or any(cls._has_glob(value) or "$" in value for value in operands):
                    unknown_delete_scope = True
                    batch = True
                    reasons.append(f"indeterminate {command_name} target")
                else:
                    delete_paths.extend(operands)
                    batch = batch or len(operands) > 1
                continue

            if command_name == "find" and (
                "-delete" in tokens
                or "-exec" in tokens
                or "-execdir" in tokens
                or "xargs" in tokens
            ):
                read_only = False
                recursive = True
                batch = True
                if "-delete" in tokens:
                    delete_paths.append(cls._find_root(tokens))
                    reasons.append("find -delete")
                else:
                    unknown_delete_scope = True
                    reasons.append("find dynamic delete expression")
                continue

            if command_name == "git":
                if "clean" in tokens[1:]:
                    read_only = False
                    recursive = True
                    batch = True
                    delete_paths.append(".")
                    reasons.append("git clean")
                elif "reset" in tokens[1:] and "--hard" in tokens[1:]:
                    read_only = False
                    batch = True
                    delete_paths.append(".")
                    reasons.append("git reset --hard")
                elif ("checkout" in tokens[1:] or "restore" in tokens[1:]) and "--" in tokens:
                    read_only = False
                    batch = True
                    marker = tokens.index("--")
                    operands = tokens[marker + 1 :]
                    delete_paths.extend(operands or ["."])
                    reasons.append("git workspace restore")

            if command_name in cls._write_commands:
                read_only = False
                operands = cls._path_operands(tokens)
                if command_name == "mkdir":
                    generated_dirs.extend(
                        value for value in operands if not cls._has_glob(value) and "$" not in value
                    )
                elif command_name == "touch":
                    created_paths.extend(
                        value for value in operands if not cls._has_glob(value) and "$" not in value
                    )
            unsafe_redirection = any(
                token in cls._redirections and not cls._is_safe_fd_dup(tokens, index)
                for index, token in enumerate(tokens)
            )
            if unsafe_redirection:
                read_only = False
                for index, token in enumerate(tokens[:-1]):
                    if token in {">", ">>"}:
                        target = tokens[index + 1]
                        if not cls._has_glob(target) and "$" not in target:
                            created_paths.append(target)

        # Python one-liners and nested shell strings can hide deletion from token-level parsing.
        if re.search(
            r"(?:shutil\s*\.\s*rmtree|os\s*\.\s*(?:remove|unlink|rmdir)|"
            r"(?:unlink|rmdir|remove)\s*\(|fs\s*\.\s*(?:rmSync|unlinkSync|rmdirSync))",
            command,
        ):
            read_only = False
            unknown_delete_scope = True
            batch = True
            reasons.append("Python filesystem deletion")

        if host_admin:
            kind: RiskKind = "host-admin"
        elif delete_paths or unknown_delete_scope:
            kind = "batch-delete" if batch or recursive else "delete"
        elif network_required:
            kind = "network"
        else:
            kind = "none"
        return CommandRisk(
            kind=kind,
            delete_paths=tuple(delete_paths),
            recursive=recursive,
            batch=batch,
            unknown_delete_scope=unknown_delete_scope,
            network_required=network_required,
            host_admin=host_admin,
            read_only=read_only,
            created_paths=tuple(created_paths),
            generated_dirs=tuple(generated_dirs),
            reserved_path=reserved_path,
            reasons=tuple(reasons),
        )

    @classmethod
    def is_explicit_read_only(cls, command: str) -> bool:
        """判断是否是文档允许的明确只读 Shell 命令组合。

        该结果只用于免去普通只读 Bash 的额外交互审批；Sandbox 仍然必须执行，且任何
        删除、重定向、网络或宿主机风险都会使该判断为 False。
        """

        try:
            segments = cls._segments(command)
        except ValueError:
            return False
        if not segments:
            return False
        for tokens in segments:
            if any(
                token in cls._redirections and not cls._is_safe_fd_dup(tokens, index)
                for index, token in enumerate(tokens)
            ):
                return False
            normalized_tokens = cls._without_safe_fd_dup(tokens)
            name = cls._basename(normalized_tokens[0])
            if name == "command":
                if len(normalized_tokens) == 3 and normalized_tokens[1] in {"-v", "-V"}:
                    continue
                return False
            if (
                name in cls._version_commands
                and normalized_tokens[1:]
                and all(token in cls._version_only_flags for token in normalized_tokens[1:])
            ):
                continue
            if name in cls._read_only_commands:
                continue
            if name == "git":
                if len(tokens) < 2 or tokens[1] not in {"status", "diff", "log"}:
                    return False
                continue
            return False
        risk = cls.analyze(command)
        return risk.read_only and not risk.destructive and not risk.network_required


__all__ = ["CommandRisk", "CommandRiskAnalyzer", "RiskKind"]
