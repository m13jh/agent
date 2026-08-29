"""一个小型、以事件日志为事实源的 Python Agent Harness。

这里集中导出最常用的公共入口，调用方可以从 ``python_agent`` 直接创建 Agent、
AgentManager、FakeAdapter 或 Session，而不必了解包内部的目录组织。
"""

from python_agent.core.agent import Agent
from python_agent.core.agent_loop import AgentLoop, RunResult
from python_agent.core.agent_manager import AgentManager
from python_agent.core.lifecycle import CancelCause
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.session.session import Session

__all__ = [
    "Agent",
    "AgentLoop",
    "AgentManager",
    "CancelCause",
    "FakeAdapter",
    "LiveEventBus",
    "RunResult",
    "Session",
]

__version__ = "0.1.0"
