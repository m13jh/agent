"""AgentManager：统一拥有、创建和释放 Agent Handle。"""

from __future__ import annotations

from pathlib import Path

from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import SessionId
from python_agent.llm.adapter import ModelAdapter, ModelRouter
from python_agent.session.session import Session
from python_agent.tools.registry import ToolRegistry


class AgentManager:
    """管理当前进程中的 Agent，并确保每个 Handle 都有明确生命周期所有者。"""

    def __init__(self, *, event_bus: LiveEventBus | None = None) -> None:
        self.event_bus = event_bus or LiveEventBus()
        self._agents: dict[SessionId, Agent] = {}

    async def create(
        self,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        session: Session | None = None,
        system_prompt: str | None = None,
        workspace: Path | None = None,
    ) -> Agent:
        """创建 Agent、登记所有权并发布 agent/created 通知。"""

        agent = Agent(
            adapter,
            tools,
            config=config,
            session=session,
            system_prompt=system_prompt,
            workspace=workspace,
            event_bus=self.event_bus,
        )
        if agent.id in self._agents:
            await agent.dispose()
            raise RuntimeError(f"agent already managed: {agent.id}")
        self._agents[agent.id] = agent
        await self.event_bus.emit(
            "agent/created",
            {
                "agent_id": str(agent.id),
                "session_id": str(agent.session.id),
                "status": agent.status,
            },
        )
        return agent

    async def create_agent(
        self,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        session: Session | None = None,
        system_prompt: str | None = None,
        workspace: Path | None = None,
    ) -> Agent:
        """create 的语义别名，方便调用方使用更明确的动词名称。"""

        return await self.create(
            adapter,
            tools,
            config=config,
            session=session,
            system_prompt=system_prompt,
            workspace=workspace,
        )

    def get(self, agent_id: SessionId) -> Agent:
        """根据稳定 ID 返回 Agent；不存在时明确失败。"""

        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"agent not found: {agent_id}") from exc

    def list_agents(self) -> list[Agent]:
        """返回当前由 Manager 拥有的 Agent 快照。"""

        return list(self._agents.values())

    async def dispose(self, agent_id: SessionId) -> None:
        """释放一个 Agent，并在释放成功后移除 Manager 所有权记录。"""

        agent = self.get(agent_id)
        await agent.dispose()
        self._agents.pop(agent_id, None)

    async def shutdown(self) -> None:
        """应用退出时释放所有 Agent，防止遗留 Driver Task。"""

        agents = self.list_agents()
        for agent in agents:
            await agent.dispose()
        self._agents.clear()


__all__ = ["AgentManager"]
