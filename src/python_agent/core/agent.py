"""阶段 1 的最小 Agent 外观。

更完整的 Agent Handle 协议（Inbox、followup、steer、inject）会在阶段 2 引入。本模块先
保留稳定的 ``Agent`` 名称，让只需要阶段 1 ``run`` 操作的调用方不必依赖内部循环类。
"""

from python_agent.core.agent_loop import AgentLoop, RunResult


class Agent(AgentLoop):
    pass


__all__ = ["Agent", "RunResult"]
