"""工具运行时共享的内部类型。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from python_agent.ids import CallId, SessionId
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
    approval_service: Any = field(
        default=None,
        metadata={"description": "可选的高风险工具审批服务"},
    )
    metadata: dict[str, Any] = field(
        default_factory=dict,
        metadata={"description": "供应用层扩展使用的非模型可见元数据"},
    )


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
