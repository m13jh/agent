"""安全发现并按需读取 Skill 指令的文件系统 Registry。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from python_agent.errors import SkillError
from python_agent.skills.types import LoadedSkill, SkillManifest, SkillMetadata


class SkillRegistry:
    """以 ``<root>/<name>/skill.toml`` 为边界管理声明式 Skill。"""

    def __init__(self, root: Path, *, max_instruction_bytes: int = 64_000) -> None:
        self.root = root.expanduser().resolve()
        self.max_instruction_bytes = max_instruction_bytes

    @staticmethod
    def _load_toml(path: Path) -> dict[str, Any]:
        """使用 Python 3.11 tomllib 或 3.10 tomli 解析纯数据。"""

        module_name = "tomllib" if sys.version_info >= (3, 11) else "tomli"
        module: Any = importlib.import_module(module_name)
        try:
            with path.open("rb") as file:
                value: Any = module.load(file)
        except (OSError, ValueError, TypeError) as exc:
            raise SkillError(f"cannot load skill manifest {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise SkillError(f"skill manifest must be an object: {path}")
        return value

    def _skill_directory(self, name: str) -> Path:
        """校验名称并确保目录没有通过符号链接逃出 Skill root。"""

        try:
            validated = SkillManifest(
                name=name,
                description="placeholder",
            ).name
        except ValidationError as exc:
            raise SkillError(f"invalid skill name {name!r}") from exc
        directory = (self.root / validated).resolve()
        try:
            directory.relative_to(self.root)
        except ValueError as exc:
            raise SkillError(f"skill path escapes root: {name}") from exc
        return directory

    def _manifest(self, directory: Path) -> SkillManifest:
        """读取清单并要求 manifest name 与目录名称一致。"""

        path = directory / "skill.toml"
        try:
            manifest = SkillManifest.model_validate(self._load_toml(path))
        except ValidationError as exc:
            raise SkillError(f"invalid skill manifest {path}: {exc}") from exc
        if manifest.name != directory.name:
            raise SkillError(
                f"skill manifest name {manifest.name!r} does not match directory {directory.name!r}"
            )
        return manifest

    def list(self) -> list[SkillMetadata]:
        """只读取清单元数据，保持完整 Skill 指令按需加载。"""

        if not self.root.exists():
            return []
        try:
            directories = sorted(
                (
                    path
                    for path in self.root.iterdir()
                    if path.is_dir() and not path.name.startswith(".")
                ),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise SkillError(f"cannot list skill root {self.root}: {exc}") from exc
        result: list[SkillMetadata] = []
        for directory in directories:
            manifest = self._manifest(self._skill_directory(directory.name))
            result.append(
                SkillMetadata(
                    name=manifest.name,
                    description=manifest.description,
                    allowed_tools=manifest.allowed_tools,
                )
            )
        return result

    def load(self, name: str, *, available_tools: set[str] | None = None) -> LoadedSkill:
        """读取单个 Skill 指令，并校验文件边界、大小与声明工具授权。"""

        directory = self._skill_directory(name)
        if not directory.is_dir():
            raise SkillError(f"skill does not exist: {name}")
        manifest = self._manifest(directory)
        requested = set(manifest.allowed_tools)
        if available_tools is not None:
            unauthorized = requested - available_tools
            if unauthorized:
                raise SkillError(
                    f"skill {name} requests unavailable tools: " + ", ".join(sorted(unauthorized))
                )
        instructions_path = (directory / manifest.instructions_file).resolve()
        try:
            instructions_path.relative_to(directory)
        except ValueError as exc:
            raise SkillError(f"skill instructions escape directory: {name}") from exc
        try:
            size = instructions_path.stat().st_size
            if size > self.max_instruction_bytes:
                raise SkillError(
                    f"skill instructions exceed {self.max_instruction_bytes} bytes: {name}"
                )
            instructions = instructions_path.read_text(encoding="utf-8")
        except SkillError:
            raise
        except (OSError, UnicodeError) as exc:
            raise SkillError(f"cannot read skill instructions {instructions_path}: {exc}") from exc
        if not instructions.strip():
            raise SkillError(f"skill instructions are empty: {name}")
        return LoadedSkill(
            name=manifest.name,
            description=manifest.description,
            instructions=instructions,
            allowed_tools=manifest.allowed_tools,
            source_path=instructions_path,
        )


__all__ = ["SkillRegistry"]
