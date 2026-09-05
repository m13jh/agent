"""当前 Agent 任务生成文件的受控清单。

文件是否 ``untracked`` 不能证明它是 Agent 可以自动删除的临时文件。本模块只接受由
受信任的文件工具在成功提交后登记的路径，并支持可选的 0600 磁盘快照，供删除策略在
同一个任务生命周期内作出保守判断。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from python_agent.errors import ToolError

_TEMPORARY_DIRECTORY_NAMES = frozenset(
    {
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "tmp",
    }
)
_TEMPORARY_SUFFIXES = frozenset({".log", ".tmp", ".temp"})


def is_obviously_temporary(path: Path) -> bool:
    """判断路径名是否属于常见的可重新生成临时产物。

    这个函数只能作为“已经被当前任务 manifest 登记”之后的第二个条件，不能单独把
    workspace 中名字像 ``tmp`` 的用户文件变成可自动删除对象。
    """

    parts = {part.lower() for part in path.parts}
    if parts & _TEMPORARY_DIRECTORY_NAMES:
        return True
    return path.name.lower() in {".coverage"} or path.suffix.lower() in _TEMPORARY_SUFFIXES


class TaskFileManifest:
    """记录当前任务创建的文件/目录及其中明确标记的临时产物。"""

    def __init__(
        self,
        task_id: str,
        workspace: Path,
        *,
        manifest_path: Path | None = None,
        persist: bool = False,
    ) -> None:
        if not task_id or not task_id.strip():
            raise ValueError("task_id must be non-empty")
        self.task_id = task_id
        self.workspace = workspace.expanduser().resolve()
        if self.workspace == Path("/"):
            raise ToolError("task manifest workspace cannot be the filesystem root")
        self.manifest_path = (
            manifest_path.expanduser().resolve()
            if manifest_path is not None
            else self.workspace
            / ".python-agent"
            / "task-manifests"
            / f"{self._safe_task_id(task_id)}.json"
        )
        self.persist = persist
        self._created_files: set[str] = set()
        self._generated_dirs: set[str] = set()
        self._temporary_files: set[str] = set()
        self._temporary_dirs: set[str] = set()

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        """把任务 ID 转为不会改变 manifest 目录结构的文件名。"""

        safe = "".join(
            character if character.isalnum() or character in "._-" else "_" for character in task_id
        )
        return safe[:128] or "task"

    def _relative(self, value: Path | str) -> tuple[Path, str]:
        """解析路径并返回 workspace 相对 Path 和稳定的 POSIX 键。"""

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError(f"task manifest path is outside workspace: {value}") from exc
        if not relative.parts:
            raise ToolError("task manifest cannot register the workspace root")
        return resolved, relative.as_posix()

    @staticmethod
    def _contains(container: str, candidate: str) -> bool:
        return candidate == container or candidate.startswith(container.rstrip("/") + "/")

    @property
    def created_files(self) -> tuple[Path, ...]:
        """返回已经登记的创建文件绝对路径快照。"""

        return tuple(
            sorted((self.workspace / relative for relative in self._created_files), key=str)
        )

    @property
    def generated_dirs(self) -> tuple[Path, ...]:
        """返回已经登记的生成目录绝对路径快照。"""

        return tuple(
            sorted((self.workspace / relative for relative in self._generated_dirs), key=str)
        )

    @property
    def temporary_files(self) -> tuple[Path, ...]:
        """返回明确标记为临时的创建文件快照。"""

        return tuple(
            sorted((self.workspace / relative for relative in self._temporary_files), key=str)
        )

    @property
    def temporary_dirs(self) -> tuple[Path, ...]:
        """返回明确标记为临时的生成目录快照。"""

        return tuple(
            sorted((self.workspace / relative for relative in self._temporary_dirs), key=str)
        )

    def record_created(self, path: Path | str, *, temporary: bool = False) -> None:
        """在文件成功创建后登记它；可选地标记为当前任务的临时产物。"""

        _, relative = self._relative(path)
        self._created_files.add(relative)
        if temporary or is_obviously_temporary(Path(relative)):
            self._temporary_files.add(relative)
        self._save_if_configured()

    def record_generated_dir(self, path: Path | str, *, temporary: bool = False) -> None:
        """登记当前任务创建的目录及其后代。"""

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        # workspace 是任务的容器而不是任务生成的目录。第一次向一个尚不存在的
        # workspace 写文件时，WriteFileTool 会先创建它；忽略这个外层目录，避免文件已经
        # 成功落盘后因为“不能登记 workspace 根”被错误报告为失败。
        if candidate.resolve() == self.workspace:
            return
        _, relative = self._relative(path)
        self._generated_dirs.add(relative)
        if temporary or is_obviously_temporary(Path(relative)):
            self._temporary_dirs.add(relative)
        self._save_if_configured()

    # 更直观的别名，方便应用层在“add”语义下使用同一清单。
    add_created_file = record_created
    add_generated_dir = record_generated_dir
    register_created_file = record_created
    register_generated_dir = record_generated_dir

    @classmethod
    def for_task(
        cls,
        workspace: Path,
        task_id: str,
        *,
        manifest_path: Path | None = None,
        persist: bool = False,
    ) -> TaskFileManifest:
        """以 workspace-first 参数顺序创建清单，方便应用层调用。"""

        return cls(
            task_id,
            workspace,
            manifest_path=manifest_path,
            persist=persist,
        )

    def is_task_generated(self, path: Path | str) -> bool:
        """判断路径是否是清单中的文件或已登记生成目录的后代。"""

        _, relative = self._relative(path)
        return relative in self._created_files or any(
            self._contains(directory, relative) for directory in self._generated_dirs
        )

    def is_temporary(self, path: Path | str) -> bool:
        """判断路径是否被清单明确标记或属于已登记的明显临时目录。"""

        _, relative = self._relative(path)
        return relative in self._temporary_files or any(
            self._contains(directory, relative) for directory in self._temporary_dirs
        )

    def is_auto_deletable(self, path: Path | str) -> bool:
        """只有“当前任务生成”且“临时/可重建”两项同时满足才允许 D0 删除。"""

        resolved, relative = self._relative(path)
        if relative in self._temporary_files:
            return True
        if relative in self._created_files:
            return self.is_temporary(resolved) or is_obviously_temporary(Path(relative))
        if relative not in self._temporary_dirs or not resolved.is_dir():
            return False
        # 目录只在其当前所有后代也由 manifest 明确登记时才是 D0。这样用户后来放入
        # 临时目录的源码/数据不会因为“父目录由 Agent 创建”而被批量自动删除。
        for child in resolved.rglob("*"):
            _, child_relative = self._relative(child)
            if child.is_dir():
                if child_relative not in self._generated_dirs:
                    return False
            elif child_relative not in self._created_files:
                return False
        return True

    def record_deleted(self, path: Path | str) -> None:
        """从清单移除已经删除的目标，避免同一路径后来被用户重建后误判为 D0。"""

        _, relative = self._relative(path)
        self._created_files.discard(relative)
        self._temporary_files.discard(relative)
        for collection in (self._generated_dirs, self._temporary_dirs):
            collection.difference_update(
                value
                for value in tuple(collection)
                if self._contains(relative, value) or self._contains(value, relative)
            )
        self._save_if_configured()

    def snapshot(self) -> dict[str, Any]:
        """返回可审计的 JSON 数据，不包含任意对象。"""

        return {
            "version": 1,
            "task_id": self.task_id,
            "workspace": str(self.workspace),
            "created_files": sorted(self._created_files),
            "generated_dirs": sorted(self._generated_dirs),
            "temporary_files": sorted(self._temporary_files),
            "temporary_dirs": sorted(self._temporary_dirs),
        }

    def save(self, path: Path | None = None) -> Path:
        """以 0600 权限原子保存 manifest 快照。"""

        destination = (path or self.manifest_path).expanduser().resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError("task manifest destination is outside workspace") from exc
        if destination == self.workspace:
            raise ToolError("task manifest destination cannot be workspace root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        payload = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, destination)
        except OSError:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise
        return destination

    def _save_if_configured(self) -> None:
        if self.persist:
            self.save()

    @classmethod
    def load(cls, path: Path) -> TaskFileManifest:
        """加载并严格校验一个已有 manifest。"""

        source = path.expanduser().resolve()
        try:
            raw: Any = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ToolError(f"cannot load task file manifest {source}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ToolError(f"invalid task file manifest: {source}")
        task_id = raw.get("task_id")
        workspace = raw.get("workspace")
        if not isinstance(task_id, str) or not isinstance(workspace, str):
            raise ToolError(f"task file manifest lacks task_id or workspace: {source}")
        manifest = cls(task_id, Path(workspace), manifest_path=source)
        for key, target in (
            ("created_files", manifest._created_files),
            ("generated_dirs", manifest._generated_dirs),
            ("temporary_files", manifest._temporary_files),
            ("temporary_dirs", manifest._temporary_dirs),
        ):
            values = raw.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ToolError(f"invalid {key} in task file manifest: {source}")
            for value in values:
                _, relative = manifest._relative(value)
                target.add(relative)
        if not manifest._temporary_files <= manifest._created_files:
            raise ToolError("temporary file is not present in created_files")
        if not manifest._temporary_dirs <= manifest._generated_dirs:
            raise ToolError("temporary directory is not present in generated_dirs")
        return manifest


__all__ = ["TaskFileManifest", "is_obviously_temporary"]
