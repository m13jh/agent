"""提供经过校验且不可变的 Agent 配置模型。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from python_agent.errors import ConfigurationError


class AgentPreset(BaseModel):
    """描述一个 Agent 的稳定能力集合和执行限制。

    配置在创建后不可变，Session Header 会记录 preset 名称，使后续恢复时不会意外获得
    与原运行不同的模型、工具或资源限制。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = "default"
    provider: str = "fake"
    model: str = "fake-model"
    max_steps: int = Field(default=30, gt=0)
    max_tokens: int = Field(default=2048, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tool_result_chars: int = Field(default=12000, gt=0)
    workspace: Path | None = None
    tools: tuple[str, ...] = ()
    permission_mode: Literal["read-only", "workspace-write"] = "read-only"


AgentConfig = AgentPreset


def load_toml(path: Path) -> AgentPreset:
    """从 TOML 文件加载 preset。

    配置文件只会被解析成数据模型，不会执行任意 Python 代码；解析失败会转换成统一的
    ``ConfigurationError``，让 CLI 和上层 API 可以给出一致的错误信息。
    """

    try:
        # Python 3.11 自带 tomllib；Python 3.10 则通过项目依赖的 tomli 提供同样的接口。
        # 使用 importlib 动态加载可以让 mypy 在两种 Python 版本下都能通过类型检查。
        module_name = "tomllib" if sys.version_info >= (3, 11) else "tomli"
        toml_module: Any = importlib.import_module(module_name)

        with path.open("rb") as file:
            raw: dict[str, Any] = toml_module.load(file)
        values = raw.get("agent", raw)
        if not isinstance(values, dict):
            raise ConfigurationError("TOML [agent] section must be an object")
        return AgentPreset.model_validate(values)
    except ConfigurationError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigurationError(f"cannot load configuration {path}: {exc}") from exc
