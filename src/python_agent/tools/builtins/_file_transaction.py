"""为 workspace 工具提供事务性且能检测过期写入的文件变更。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from python_agent.errors import ToolError


@dataclass(slots=True)
class FileChange:
    """已经准备好的文件替换/删除操作，以及不可变的前置条件快照。"""

    path: Path
    content: bytes | None
    existed: bool
    original_bytes: bytes | None
    original_mode: int | None
    original_mtime_ns: int | None
    original_sha256: str | None

    @classmethod
    def capture(cls, path: Path, content: bytes | None) -> FileChange:
        """在修改目标或父目录之前捕获目标状态。"""

        try:
            metadata = path.stat()
        except FileNotFoundError:
            return cls(path, content, False, None, None, None, None)
        except OSError as exc:
            raise ToolError(f"cannot inspect file {path}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolError(f"file transaction target is not a regular file: {path}")
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read file {path}: {exc}") from exc
        return cls(
            path=path,
            content=content,
            existed=True,
            original_bytes=original,
            original_mode=stat.S_IMODE(metadata.st_mode),
            original_mtime_ns=metadata.st_mtime_ns,
            original_sha256=hashlib.sha256(original).hexdigest(),
        )

    def assert_unchanged(self) -> None:
        """拒绝准备和提交之间发生的外部修改。"""

        try:
            metadata = self.path.stat()
        except FileNotFoundError:
            if self.existed:
                raise ToolError(f"stale write: file disappeared before commit: {self.path}")
            return
        except OSError as exc:
            raise ToolError(f"cannot inspect file before commit {self.path}: {exc}") from exc
        if not self.existed:
            raise ToolError(f"stale write: file appeared before commit: {self.path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolError(f"stale write: target is no longer a regular file: {self.path}")
        try:
            current = self.path.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read file before commit {self.path}: {exc}") from exc
        current_hash = hashlib.sha256(current).hexdigest()
        if metadata.st_mtime_ns != self.original_mtime_ns or current_hash != self.original_sha256:
            raise ToolError(f"stale write: file changed before commit: {self.path}")


class FileTransaction:
    """先暂存全部变更，再作为一个逻辑操作提交；失败时尽力回滚。"""

    def __init__(self, root: Path, changes: list[tuple[Path, bytes | None]]) -> None:
        self.root = root.expanduser().resolve()
        if len({path for path, _ in changes}) != len(changes):
            raise ToolError("file transaction contains duplicate targets")
        self.changes = [FileChange.capture(path, content) for path, content in changes]
        self._validate_parent_paths()

    def _validate_parent_paths(self) -> None:
        """在创建暂存数据或修改目标之前检查每个父目录链。"""

        for change in self.changes:
            if change.content is None and not change.existed:
                raise ToolError(f"cannot delete missing file: {change.path}")
            if change.content is not None and change.existed is False:
                # Add 操作允许目标不存在，因为捕获时目标就是不存在的。
                pass
            current = change.path.parent
            while True:
                try:
                    metadata = current.stat()
                except FileNotFoundError:
                    if current == current.parent:
                        break
                    current = current.parent
                    continue
                except OSError as exc:
                    raise ToolError(f"cannot inspect parent directory {current}: {exc}") from exc
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ToolError(f"file transaction parent is not a directory: {current}")
                break

    @staticmethod
    def _write_durable(path: Path, content: bytes, mode: int) -> None:
        """写入私有临时文件并 fsync，之后才让文件对外可见。"""

        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as file:
                descriptor = -1
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @classmethod
    def recover_pending(cls, root: Path) -> None:
        """恢复进程崩溃后、尚未写入 committed 标记的事务。

        多文件 rename 无法成为一个文件系统原子操作。持久化 manifest 让事务可以恢复：下一次
        workspace 文件操作会先恢复事务前快照，并删除事务中新建的目标，然后再继续。带有
        committed 标记的事务已经完成，只需删除私有暂存目录。
        """

        transaction_root = root.expanduser().resolve() / ".python-agent" / "transactions"
        if not transaction_root.is_dir():
            return
        for directory in sorted(transaction_root.iterdir(), key=lambda path: path.name):
            if not directory.is_dir():
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.is_file():
                shutil.rmtree(directory, ignore_errors=True)
                continue
            if (directory / "COMMITTED").is_file():
                shutil.rmtree(directory, ignore_errors=True)
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                changes = manifest["changes"]
                if not isinstance(changes, list):
                    raise ValueError("changes must be a list")
                for raw_change in reversed(changes):
                    if not isinstance(raw_change, dict):
                        raise ValueError("transaction change must be an object")
                    relative = raw_change["path"]
                    if not isinstance(relative, str):
                        raise ValueError("transaction path must be text")
                    target = (root / relative).resolve()
                    try:
                        target.relative_to(root.resolve())
                    except ValueError as exc:
                        raise ValueError("transaction target escapes workspace") from exc
                    existed = bool(raw_change["existed"])
                    if existed:
                        backup_name = raw_change["backup"]
                        backup_path = directory / backup_name
                        original = backup_path.read_bytes()
                        cls._restore_bytes(
                            target,
                            original,
                            int(raw_change.get("mode") or 0o600),
                        )
                    elif target.exists() or target.is_symlink():
                        new_hash = raw_change.get("new_sha256")
                        if isinstance(new_hash, str) and target.is_file():
                            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                            if current_hash != new_hash:
                                continue
                        target.unlink()
                        cls._fsync_directory(target.parent)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise ToolError(f"cannot recover file transaction {directory}: {exc}") from exc
            shutil.rmtree(directory, ignore_errors=True)

    @classmethod
    def _restore_bytes(cls, path: Path, content: bytes, mode: int) -> None:
        """从事务备份中原子恢复一个文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.restore-",
            dir=path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            cls._write_durable(temporary, content, mode)
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _rollback(self, applied: list[FileChange], created_directories: list[Path]) -> None:
        """按逆序恢复已经提交的目标。"""

        rollback_errors: list[str] = []
        for change in reversed(applied):
            try:
                if change.existed:
                    assert change.original_bytes is not None
                    self._restore_bytes(
                        change.path,
                        change.original_bytes,
                        change.original_mode or 0o600,
                    )
                elif change.path.exists() or change.path.is_symlink():
                    change.path.unlink()
                    self._fsync_directory(change.path.parent)
            except OSError as exc:
                rollback_errors.append(f"{change.path}: {exc}")
        for directory in sorted(
            created_directories,
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise ToolError("file transaction rollback failed: " + "; ".join(rollback_errors))

    def commit(self) -> None:
        """暂存每个替换，提交全部目标；任一失败就回滚。"""

        self.root.mkdir(parents=True, exist_ok=True)
        self.recover_pending(self.root)
        transaction_root = self.root / ".python-agent" / "transactions"
        transaction_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(transaction_root, 0o700)
        except OSError:
            pass
        staging = Path(tempfile.mkdtemp(prefix="tx-", dir=transaction_root))
        try:
            try:
                os.chmod(staging, 0o700)
            except OSError:
                pass
            staged: dict[Path, Path] = {}
            manifest_changes: list[dict[str, object]] = []
            for index, change in enumerate(self.changes):
                backup_name = f"{index}.backup"
                if change.existed:
                    assert change.original_bytes is not None
                    self._write_durable(
                        staging / backup_name,
                        change.original_bytes,
                        0o600,
                    )
                if change.content is None:
                    new_sha256 = None
                else:
                    new_sha256 = hashlib.sha256(change.content).hexdigest()
                    stage_path = staging / f"{index}.tmp"
                    self._write_durable(
                        stage_path,
                        change.content,
                        change.original_mode if change.existed and change.original_mode else 0o600,
                    )
                    staged[change.path] = stage_path
                try:
                    relative_path = change.path.relative_to(self.root).as_posix()
                except ValueError as exc:
                    raise ToolError(
                        f"transaction target is outside workspace: {change.path}"
                    ) from exc
                manifest_changes.append(
                    {
                        "path": relative_path,
                        "existed": change.existed,
                        "mode": change.original_mode,
                        "backup": backup_name,
                        "new_sha256": new_sha256,
                    }
                )

            # 暂存后重新检查全部 hash，避免慢速模型/工具或文件观察者在准备期间的修改被
            # 静默覆盖。
            for change in self.changes:
                change.assert_unchanged()

            manifest = json.dumps(
                {"version": 1, "changes": manifest_changes},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            self._write_durable(staging / "manifest.json", manifest, 0o600)
            self._fsync_directory(staging)
            self._write_durable(staging / "COMMITTING", b"1\n", 0o600)
            self._fsync_directory(staging)

            applied: list[FileChange] = []
            created_directories: list[Path] = []
            try:
                for change in self.changes:
                    parent = change.path.parent
                    missing: list[Path] = []
                    cursor = parent
                    while not cursor.exists():
                        missing.append(cursor)
                        cursor = cursor.parent
                    parent.mkdir(parents=True, exist_ok=True)
                    created_directories.extend(reversed(missing))
                    if change.content is None:
                        change.path.unlink()
                    else:
                        os.replace(staged[change.path], change.path)
                    self._fsync_directory(parent)
                    applied.append(change)
                self._write_durable(staging / "COMMITTED", b"1\n", 0o600)
                self._fsync_directory(staging)
            except Exception as exc:
                try:
                    self._rollback(applied, created_directories)
                except Exception as rollback_error:
                    raise rollback_error from exc
                if isinstance(exc, ToolError):
                    raise
                raise ToolError(f"file transaction commit failed: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


__all__ = ["FileChange", "FileTransaction"]
