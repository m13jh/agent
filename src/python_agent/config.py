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

    id: str = Field(default="default", description="用于恢复时识别能力集合的 preset 名称")
    provider: str = Field(default="fake", description="模型 Provider 路由名称")
    model: str = Field(default="fake-model", description="Provider 接受的模型名称")
    max_steps: int = Field(default=30, gt=0, description="一个 Turn 最多执行的模型步骤数")
    max_tokens: int = Field(default=2048, gt=0, description="单次模型响应的最大输出 Token 数")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="模型采样温度")
    max_parallel_tools: int = Field(
        default=4,
        gt=0,
        le=64,
        description="同一批并发安全工具最多同时执行的数量",
    )
    max_turn_tokens: int | None = Field(
        default=None,
        gt=0,
        description="一个 Turn 内所有模型请求累计允许消耗的总 Token；None 表示不限制",
    )
    max_turn_cost_usd: float | None = Field(
        default=None,
        gt=0,
        description="一个 Turn 的最大美元费用；需要 Provider cost 或显式 Token 单价",
    )
    max_turn_seconds: float | None = Field(
        default=None,
        gt=0,
        description="一个 Turn 从开始到结束允许占用的最大墙钟秒数",
    )
    input_cost_per_million_tokens: float | None = Field(
        default=None,
        ge=0,
        description="Provider 未返回费用时，用于估算输入 Token 成本的每百万 Token 单价",
    )
    output_cost_per_million_tokens: float | None = Field(
        default=None,
        ge=0,
        description="Provider 未返回费用时，用于估算输出 Token 成本的每百万 Token 单价",
    )
    model_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="一次 Step 的模型请求失败后最多重试次数，不含首次请求",
    )
    model_retry_base_delay_seconds: float = Field(
        default=0.5,
        ge=0,
        le=60,
        description="模型重试指数退避的基础秒数",
    )
    max_tool_result_chars: int = Field(
        default=12000,
        gt=0,
        description="工具结果进入模型上下文前允许保留的最大字符数",
    )
    workspace: Path | None = Field(default=None, description="工具可访问的 workspace 根目录")
    tools: tuple[str, ...] = Field(default=(), description="允许暴露给模型的工具名称")
    permission_mode: Literal["read-only", "workspace-write"] = Field(
        default="read-only",
        description="工具权限模式；写工具和 Bash 需要 workspace-write",
    )
    approval_required: tuple[str, ...] = Field(
        default=("bash",),
        description="必须经过 ApprovalService 的高风险工具名称",
    )
    subagents_enabled: bool = Field(
        default=False,
        description="是否为此 Agent 注册进程内子 Agent 管理工具",
    )
    max_delegation_depth: int = Field(
        default=2,
        ge=0,
        le=16,
        description="允许的最大 Session delegation_depth；根 Agent 深度为 0",
    )
    max_subagents: int = Field(
        default=8,
        gt=0,
        le=128,
        description="一个直接父 Agent 在当前进程中最多创建的子 Agent 数量",
    )
    skills_root: Path | None = Field(
        default=None,
        description="可按需加载的 Skill 根目录；None 表示不暴露 Skill 工具",
    )


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
