"""Session 的只追加事件日志，以及从事件派生的消息和 transcript 投影。

Session 模块只负责记录和投影，不负责调用模型或执行工具，这样日志可以独立回放。
"""

from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.projection import derive_messages
from python_agent.session.session import Session

__all__ = ["Session", "SessionEvent", "SessionHeader", "derive_messages"]
