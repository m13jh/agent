"""AgentManager、Driver 状态和异步 Live Event Bus 的测试。"""

import asyncio

from python_agent.core.agent import Agent
from python_agent.core.agent_manager import AgentManager
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.llm.types import AssistantResponse, ModelRequest


class ImmediateAdapter:
    """立即返回回答的模型，用于测试 Manager 和异步事件观察者。"""

    name = "immediate"

    async def complete(
        self, request: ModelRequest, *, cancel_event: asyncio.Event
    ) -> AssistantResponse:
        """返回固定回答；测试重点是生命周期通知，而不是模型内容。"""

        return AssistantResponse(content="done")


async def test_manager_owns_agent_and_async_status_observer() -> None:
    """验证 Manager 登记 Agent，并等待异步 status 监听器完成。"""

    bus = LiveEventBus()
    created: list[str] = []
    statuses: list[str] = []
    bus.subscribe("agent/created", lambda event_type, data: created.append(data["agent_id"]))

    async def observe_status(event_type: str, data: dict) -> None:
        """主动 yield，验证 when_idle 会等待异步状态监听器完成。"""

        await asyncio.sleep(0)
        statuses.append(data["status"])

    bus.subscribe("agent/status", observe_status)
    manager = AgentManager(event_bus=bus)
    agent = await manager.create(ImmediateAdapter())

    assert isinstance(agent, Agent)
    assert created == [str(agent.id)]
    await agent.run("hello")
    assert statuses[-2:] == ["running", "idle"]

    await manager.dispose(agent.id)
    assert manager.list_agents() == []
