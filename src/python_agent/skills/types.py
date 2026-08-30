"""按需 Skill 的清单、元数据和加载结果类型。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SkillManifest(BaseModel):
    """skill.toml 的严格声明；配置只描述数据，不执行 Python。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    description: str = Field(min_length=1)
    instructions_file: str = Field(default="SKILL.md", min_length=1)
    allowed_tools: tuple[str, ...] = ()


class SkillMetadata(BaseModel):
    """list_skills 返回的轻量元数据，不包含完整指令。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    allowed_tools: tuple[str, ...]


class LoadedSkill(BaseModel):
    """load_skill 返回并通过 tool/result 进入模型上下文的内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    source_path: Path


__all__ = ["LoadedSkill", "SkillManifest", "SkillMetadata"]
