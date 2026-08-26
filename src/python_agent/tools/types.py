"""工具运行时共享的内部类型。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from python_agent.ids import SessionId


@dataclass(slots=True)
class ToolContext:
    session_id: SessionId
    workspace: Path | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    metadata: dict[str, Any] = field(default_factory=dict)
