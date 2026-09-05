"""统一文件、补丁和 Shell 删除请求的风险决策引擎。

Sandbox 只回答“路径/命令在技术上是否被隔离”，本模块回答“当前任务是否可以删除”。
所有删除入口都先经过同一个引擎；无法证明目标范围、任务归属或 Git 状态时默认进入
审批，敏感路径和 workspace 根目录则直接拒绝。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from python_agent.approval.service import ApprovalRequest, ApprovalService
from python_agent.errors import ToolError
from python_agent.ids import CallId
from python_agent.tools.task_manifest import TaskFileManifest

if TYPE_CHECKING:
    from python_agent.tools.types import ToolContext

DeleteAction = Literal["allow", "approval", "deny"]
DeleteRisk = Literal[
    "auto-delete",
    "user-existing-file",
    "batch-delete",
    "critical",
    "unknown",
]
DeleteRiskLevel = DeleteRisk


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    """已经解析的删除请求；Shell 风险分析器也转换成这个形状。"""

    paths: tuple[str, ...]
    recursive: bool = False
    batch: bool = False
    source: str = "file"


@dataclass(frozen=True, slots=True)
class DeleteDecision:
    """删除策略的可审计决定。"""

    action: DeleteAction
    risk: DeleteRisk
    reason: str
    paths: tuple[Path, ...] = ()
    recursive: bool = False
    batch: bool = False
    soft_delete: bool = True
    approved: bool = False
    member_count: int = 0

    @property
    def allowed(self) -> bool:
        """是否可以进入实际删除执行器。"""

        return self.action == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.action == "approval"

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    @property
    def risk_level(self) -> str:
        """返回文档中的 D0/D1/批量/critical 标签。"""

        return {
            "auto-delete": "D0",
            "user-existing-file": "D1",
            "batch-delete": "BATCH",
            "critical": "CRITICAL",
            "unknown": "UNKNOWN",
        }[self.risk]


class GitStatusProvider:
    """用 ``git status --porcelain`` 判断目标是否包含用户未提交改动。"""

    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("git status timeout must be positive")
        self.timeout_seconds = timeout_seconds

    def status(self, workspace: Path) -> dict[str, str] | None:
        """返回相对路径到 porcelain 状态的映射；无法确定时返回 None。"""

        if not (workspace / ".git").exists():
            return {}
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "--no-optional-locks",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        statuses: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            state = line[:2]
            raw_path = line[3:]
            # Rename/copy 的 porcelain 形式为 old -> new；两个名字都视为受保护。
            values = [part.strip() for part in raw_path.split(" -> ")]
            for value in values:
                statuses[Path(value).as_posix()] = state
        return statuses

    def is_dirty(self, workspace: Path, path: Path) -> bool | None:
        """判断一个目标是否在 Git 状态中出现。"""

        statuses = self.status(workspace)
        if statuses is None:
            return None
        try:
            relative = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            return None
        if relative in statuses:
            return True
        prefix = relative.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in statuses)


class DeletePolicyEngine:
    """所有删除入口共享的风险评估、审批和软删除执行器。"""

    _sensitive_exact = frozenset(
        {
            ".env",
            ".npmrc",
            ".pypirc",
            "credentials",
            "credentials.json",
            "service-account.json",
            "id_rsa",
            "id_ed25519",
        }
    )
    _sensitive_directories = frozenset({".ssh", ".aws", ".gnupg", ".azure", "gcloud"})

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        manifest: TaskFileManifest | None = None,
        approval_service: ApprovalService | None = None,
        git_status: GitStatusProvider | None = None,
        trash_directory: Path | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve() if workspace is not None else None
        if self.workspace == Path("/"):
            raise ToolError("delete policy workspace cannot be the filesystem root")
        self.manifest = manifest
        self.approval_service = approval_service
        self.git_status = git_status or GitStatusProvider()
        self.trash_directory = (
            trash_directory.expanduser().resolve() if trash_directory is not None else None
        )

    def _root(self, context: ToolContext | None) -> Path:
        root = self.workspace or (context.workspace if context is not None else None) or Path.cwd()
        resolved = root.expanduser().resolve()
        if resolved == Path("/"):
            raise ToolError("delete policy refuses the filesystem root as workspace")
        return resolved

    @staticmethod
    def _lexical_path(value: str | Path, root: Path) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else root / candidate

    @classmethod
    def _sensitive(cls, path: Path, root: Path) -> bool:
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError:
            relative = path.resolve()
        parts = {part.lower() for part in relative.parts}
        if parts & cls._sensitive_directories:
            return True
        name = path.name.lower()
        if name in {".env.example", ".env.sample", ".env.template"}:
            return False
        return (
            name in cls._sensitive_exact
            or name.startswith(".env.")
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        )

    @staticmethod
    def _is_git_metadata(relative: Path) -> bool:
        return bool(relative.parts) and relative.parts[0] == ".git"

    def _resolve_one(
        self,
        value: str | Path,
        root: Path,
    ) -> tuple[Path, Path, DeleteDecision | None]:
        """返回词法路径、真实路径和可选的立即拒绝决定。"""

        lexical = self._lexical_path(value, root)
        try:
            resolved = lexical.resolve()
        except (OSError, RuntimeError) as exc:
            return (
                lexical,
                lexical,
                DeleteDecision(
                    "deny",
                    "critical",
                    f"cannot resolve delete target {value}: {exc}",
                ),
            )
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            return (
                lexical,
                resolved,
                DeleteDecision(
                    "deny",
                    "critical",
                    f"delete target is outside workspace: {value}",
                ),
            )
        if not relative.parts:
            return (
                lexical,
                resolved,
                DeleteDecision(
                    "deny",
                    "critical",
                    "deleting the workspace root is forbidden",
                ),
            )
        if self._is_git_metadata(relative):
            return (
                lexical,
                resolved,
                DeleteDecision(
                    "deny",
                    "critical",
                    "deleting .git or its contents is forbidden",
                ),
            )
        if relative.parts[0] in {".agent-trash", ".python-agent"}:
            return (
                lexical,
                resolved,
                DeleteDecision(
                    "deny",
                    "critical",
                    "deleting agent infrastructure is forbidden",
                ),
            )
        if self._sensitive(resolved, root):
            return (
                lexical,
                resolved,
                DeleteDecision(
                    "deny",
                    "critical",
                    f"deleting sensitive credential path is forbidden: {value}",
                ),
            )
        # A recursive operation must never follow a symlink to an unknown directory.
        if lexical.is_symlink() and lexical.resolve() != lexical.absolute():
            return (
                lexical,
                resolved,
                DeleteDecision(
                    "deny",
                    "critical",
                    f"deleting a symlink target is forbidden: {value}",
                ),
            )
        return lexical, resolved, None

    def _members(self, lexical: Path, resolved: Path, *, recursive: bool) -> list[Path]:
        """列举风险判断所需的目标集合，不跟随目录内的外部符号链接。"""

        if not lexical.exists() and not lexical.is_symlink():
            return []
        if not recursive or not lexical.is_dir() or lexical.is_symlink():
            return [lexical]
        return [lexical, *sorted(lexical.rglob("*"), key=str)]

    def _auto_deletable(self, paths: Iterable[Path], context: ToolContext | None) -> bool:
        manifest = self.manifest
        if manifest is None and context is not None:
            candidate = context.task_manifest
            manifest = candidate if isinstance(candidate, TaskFileManifest) else None
        if manifest is None:
            return False
        try:
            return all(manifest.is_auto_deletable(path) for path in paths)
        except ToolError:
            return False

    @staticmethod
    def _dirty_from_status(
        statuses: dict[str, str] | None,
        root: Path,
        path: Path,
    ) -> bool | None:
        """从一次 Git 快照判断单个文件/目录，避免批量删除启动大量 git 子进程。"""

        if statuses is None:
            return None
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
        if relative in statuses:
            return True
        prefix = relative.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in statuses)

    def evaluate(
        self,
        paths: str | Path | Iterable[str | Path] | DeleteRequest,
        *,
        context: ToolContext | None = None,
        recursive: bool = False,
        batch: bool = False,
        source: str = "file",
    ) -> DeleteDecision:
        """评估删除风险；只读，不修改文件，也不请求审批。"""

        if isinstance(paths, DeleteRequest):
            request = paths
            values: tuple[str | Path, ...] = request.paths
            recursive = request.recursive
            batch = request.batch
        elif isinstance(paths, (str, Path)):
            values = (paths,)
        else:
            values = tuple(paths)
        if not values:
            return DeleteDecision("deny", "unknown", "delete target scope is empty")
        try:
            root = self._root(context)
        except ToolError as exc:
            return DeleteDecision("deny", "critical", str(exc))

        lexical_paths: list[Path] = []
        resolved_paths: list[Path] = []
        for value in values:
            lexical, resolved, immediate = self._resolve_one(value, root)
            if immediate is not None:
                return immediate
            if context is not None:
                for excluded in context.excluded_paths:
                    excluded_root = excluded.expanduser().resolve()
                    if resolved == excluded_root or excluded_root in resolved.parents:
                        return DeleteDecision(
                            "deny",
                            "critical",
                            f"delete target is reserved for agent infrastructure: {value}",
                        )
            lexical_paths.append(lexical)
            resolved_paths.append(resolved)

        if len(lexical_paths) > 1:
            batch = True
        if recursive and len(lexical_paths) > 1:
            # 删除父目录时不再重复处理其子路径，否则软删除/恢复会在父项移动后
            # 对已经不存在的子项报错，或者让批量操作产生部分结果。
            selected: list[Path] = []
            for path in sorted(lexical_paths, key=lambda item: (len(item.parts), str(item))):
                if any(parent != path and parent in path.parents for parent in selected):
                    continue
                selected.append(path)
            lexical_paths = selected
            resolved_paths = [path.resolve() for path in lexical_paths]
        members: list[Path] = []
        for lexical, resolved in zip(lexical_paths, resolved_paths):
            members.extend(self._members(lexical, resolved, recursive=recursive))
        if not members or any(not path.exists() and not path.is_symlink() for path in members):
            return DeleteDecision("deny", "unknown", "delete target does not exist")
        if any(self._sensitive(path, root) for path in members):
            return DeleteDecision("deny", "critical", "delete target includes a sensitive path")

        auto = self._auto_deletable(members, context)
        if auto:
            return DeleteDecision(
                "allow",
                "auto-delete",
                "all targets were created by the current task and are temporary/rebuildable",
                tuple(lexical_paths),
                recursive=recursive,
                batch=batch,
                soft_delete=False,
                member_count=len(members),
            )

        statuses = self.git_status.status(root)
        dirty: bool | None = False
        for path in members:
            state = self._dirty_from_status(statuses, root, path)
            if state is None:
                dirty = None
                break
            dirty = dirty or state
        if dirty is None:
            reason = "Git status could not be determined; deletion requires explicit approval"
            risk: DeleteRisk = "unknown"
        elif dirty:
            reason = "target contains user-owned uncommitted changes; explicit approval required"
            risk = "batch-delete" if batch or recursive else "user-existing-file"
        else:
            reason = "target is not proven to be a current-task temporary artifact"
            risk = "batch-delete" if batch or recursive else "user-existing-file"
        return DeleteDecision(
            "approval",
            risk,
            reason,
            tuple(lexical_paths),
            recursive=recursive,
            batch=batch,
            soft_delete=True,
            member_count=len(members),
        )

    async def authorize(
        self,
        paths: str | Path | Iterable[str | Path] | DeleteRequest,
        *,
        context: ToolContext | None = None,
        call_id: CallId | str = "delete",
        tool_name: str = "delete_file",
        arguments: dict[str, Any] | None = None,
        recursive: bool = False,
        batch: bool = False,
        source: str = "file",
    ) -> DeleteDecision:
        """评估并在需要时请求一次明确审批；审批失败仍然 fail closed。"""

        decision = self.evaluate(
            paths,
            context=context,
            recursive=recursive,
            batch=batch,
            source=source,
        )
        if decision.action != "approval":
            return decision
        service = self.approval_service
        if service is None and context is not None:
            candidate = context.approval_service
            if isinstance(candidate, ApprovalService):
                service = candidate
        if service is None:
            return replace(decision, action="deny", reason=f"approval denied: {decision.reason}")
        payload = dict(arguments or {})
        payload.setdefault("paths", [str(path) for path in decision.paths])
        payload.setdefault("recursive", decision.recursive)
        payload.setdefault("batch", decision.batch)
        request = ApprovalRequest(
            call_id=CallId(str(call_id)),
            tool_name=tool_name,
            arguments=payload,
            reason=decision.reason,
        )
        try:
            approved = bool(await service.request(request))
        except Exception as exc:
            return replace(
                decision,
                action="deny",
                reason=f"approval denied: {type(exc).__name__}: {exc}",
            )
        if not approved:
            return replace(decision, action="deny", reason=f"approval denied: {decision.reason}")
        return replace(decision, action="allow", approved=True)

    # ``check`` 是给策略和第三方工具使用的同步别名；它不会触发用户交互。
    check = evaluate
    assess = evaluate
    decide = evaluate

    def execute(
        self,
        decision: DeleteDecision,
        *,
        task_id: str = "task",
        context: ToolContext | None = None,
    ) -> list[Path]:
        """执行已授权决定；D1/批量删除默认移动到可恢复 trash。"""

        if not decision.allowed:
            raise ToolError(decision.reason)
        root = self._root(context)
        manifest = self.manifest
        if manifest is None and context is not None:
            candidate = context.task_manifest
            manifest = candidate if isinstance(candidate, TaskFileManifest) else None
        deleted: list[Path] = []
        for path in decision.paths:
            target, _resolved, immediate = self._resolve_one(path, root)
            if immediate is not None:
                raise ToolError(immediate.reason)
            if not target.exists() and not target.is_symlink():
                raise ToolError(f"delete target does not exist: {target}")
            if decision.soft_delete:
                deleted.append(self._soft_delete(target, root, task_id))
            elif decision.recursive and target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
                deleted.append(target)
            else:
                target.unlink()
                deleted.append(target)
            if manifest is not None:
                manifest.record_deleted(target)
        return deleted

    def _soft_delete(self, target: Path, root: Path, task_id: str) -> Path:
        """把用户文件移动到 workspace 内的 task 专属恢复目录。"""

        try:
            relative = target.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ToolError(f"soft-delete target is outside workspace: {target}") from exc
        trash_root = self.trash_directory or root / ".agent-trash"
        if trash_root == root:
            raise ToolError("invalid soft-delete trash directory")
        if trash_root.is_symlink():
            raise ToolError("invalid soft-delete trash directory symlink")
        try:
            trash_root.resolve().relative_to(root)
        except ValueError as exc:
            raise ToolError("invalid soft-delete trash directory") from exc
        safe_task = (
            "".join(
                character if character.isalnum() or character in "._-" else "_"
                for character in task_id
            )[:128]
            or "task"
        )
        destination = trash_root / safe_task / relative
        task_trash = trash_root / safe_task
        if task_trash.exists() and task_trash.is_symlink():
            raise ToolError("invalid soft-delete task trash symlink")
        if destination.exists() or destination.is_symlink():
            index = 1
            while True:
                candidate = destination.with_name(f"{destination.name}.deleted-{index}")
                if not candidate.exists() and not candidate.is_symlink():
                    destination = candidate
                    break
                index += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        for directory in (trash_root, task_trash, destination.parent):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        try:
            destination.parent.resolve().relative_to(root)
        except ValueError as exc:
            raise ToolError("invalid soft-delete destination") from exc
        shutil.move(str(target), str(destination))
        return destination


__all__ = [
    "DeleteAction",
    "DeleteDecision",
    "DeletePolicyEngine",
    "DeleteRequest",
    "DeleteRisk",
    "DeleteRiskLevel",
    "GitStatusProvider",
]
