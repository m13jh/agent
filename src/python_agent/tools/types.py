"""工具运行时共享的内部类型。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from python_agent.ids import CallId, SessionId
from python_agent.tools.capabilities import (
    FILESYSTEM_WORKSPACE_WRITE,
    NETWORK_INTERNET,
    PROCESS_EXECUTE,
    NetworkMode,
    NetworkModeName,
    PermissionLevel,
    PermissionLevelName,
    capabilities_for_level,
    permission_level_for,
)
from python_agent.tools.serialization import to_json_safe


@dataclass(slots=True)
class ToolContext:
    """一次工具执行可使用的运行时上下文。

    Context 是内部类型化对象，不会直接发送给模型。它携带 Session 身份、workspace、
    取消信号、权限模式和审批服务，业务工具据此完成实际操作，统一策略则在工具主体
    之前读取这些信息。
    """

    session_id: SessionId
    workspace: Path | None = field(
        default=None,
        metadata={"description": "本次工具调用允许访问的 workspace 根目录"},
    )
    cancel_event: asyncio.Event = field(
        default_factory=asyncio.Event,
        metadata={"description": "协作式取消信号；工具应在长操作中主动检查"},
    )
    permission_mode: Literal["read-only", "workspace-write"] = field(
        default="read-only",
        metadata={"description": "当前工具调用的文件和 Shell 权限模式"},
    )
    excluded_paths: tuple[Path, ...] = field(
        default=(),
        metadata={"description": "基础设施或敏感数据目录；文件工具不得读取、列出或修改"},
    )
    permission_level: PermissionLevelName | PermissionLevel | None = field(
        default=None,
        metadata={"description": "SANBOX L0-L4 权限等级；None 根据 permission_mode 映射"},
    )
    network_mode: NetworkModeName | NetworkMode | None = field(
        default=None,
        metadata={
            "description": (
                "独立网络模式；None 保持旧版低层 API 的兼容行为，Agent 默认传入 disabled"
            )
        },
    )
    granted_capabilities: frozenset[str] | None = field(
        default=None,
        metadata={"description": "可选显式 Capability 快照；由受信任 Gateway 设置"},
    )
    capabilities: frozenset[str] | None = field(
        default=None,
        metadata={"description": "granted_capabilities 的兼容别名"},
    )
    task_manifest: Any = field(
        default=None,
        metadata={"description": "当前任务生成文件清单；不直接发送给模型"},
    )
    delete_policy: Any = field(
        default=None,
        metadata={"description": "共享 DeletePolicyEngine；不直接发送给模型"},
    )
    sandbox_spec: Any = field(
        default=None,
        metadata={"description": "当前工具执行使用的 SandboxSpec；不直接发送给模型"},
    )
    approval_service: Any = field(
        default=None,
        metadata={"description": "可选的高风险工具审批服务"},
    )
    metadata: dict[str, Any] = field(
        default_factory=dict,
        metadata={"description": "供应用层扩展使用的非模型可见元数据"},
    )

    def effective_permission_level(self) -> PermissionLevel:
        """返回当前上下文的基础权限等级。"""

        return permission_level_for(self.permission_mode, self.permission_level)

    def effective_capabilities(self) -> frozenset[str]:
        """返回 Gateway 应用于本次调用的 Capability 快照。"""

        explicit = (
            self.granted_capabilities
            if self.granted_capabilities is not None
            else self.capabilities
        )
        if explicit is not None:
            capabilities = frozenset(explicit)
        else:
            capabilities = capabilities_for_level(
                self.effective_permission_level(),
                network_mode=self.network_mode,
            )
        if self.network_mode in {None, NetworkMode.DISABLED, "disabled"}:
            capabilities = frozenset(
                capability for capability in capabilities if capability != NETWORK_INTERNET
            )
        if self.permission_mode == "read-only":
            # 显式 filesystem read-only 是独立约束，即使调用方误把基础等级写成 L2/L3，
            # 也不能因此获得 workspace write 或普通 process.execute。
            capabilities = frozenset(
                capability
                for capability in capabilities
                if capability not in {FILESYSTEM_WORKSPACE_WRITE, PROCESS_EXECUTE}
            )
        return capabilities


class ToolResult(BaseModel):
    """工具流水线统一返回的模型可见结果。"""

    model_config = ConfigDict(extra="forbid")

    call_id: CallId
    name: str
    content: Any = None
    is_error: bool = False
    concludes_turn: bool = False

    def event_data(self) -> dict[str, Any]:
        """转换成可以直接写入 ``tool/result`` 事件的数据。"""

        return {
            "call_id": str(self.call_id),
            "name": self.name,
            # Runtime 的 Post 阶段通常已经完成归一化。这里仍在事件边界再次保护，覆盖直接
            # 构造 ToolResult 或在测试中绕过 Runtime 的调用方。
            "content": to_json_safe(self.content),
            "is_error": self.is_error,
            "concludes_turn": self.concludes_turn,
        }
