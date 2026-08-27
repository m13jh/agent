import asyncio

from python_agent.core.agent import Agent
from python_agent.core.inbox import Inbox
from python_agent.core.lifecycle import CancelCause
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.llm.types import AssistantResponse, ModelRequest
from python_agent.session.session import Session


class GatedAdapter:
    """可控制的 complete-only 模型，用于稳定测试 Driver 的并发和取消边界。"""

    name = "gated"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def complete(
        self, request: ModelRequest, *, cancel_event: asyncio.Event
    ) -> AssistantResponse:
        self.requests.append(request)
        self.started.set()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self.release.wait()
            return AssistantResponse(content="完成")
        finally:
            self.active -= 1


def test_inbox_replays_unclaimed_messages() -> None:
    session = Session.new()
    inbox = Inbox(session)
    first = inbox.append("first", "followup")
    inbox.append("silent", "inject")
    third = inbox.append("old", "followup")
    assert inbox.replace(third, "new") is True
    assert inbox.claim_next_turn() is not None

    restored = Inbox(session, replay=True)

    assert [item.content for item in restored.pending("next_turn")] == ["new"]
    assert [item.content for item in restored.pending("next_step")] == ["silent"]
    assert any(
        event.type == "agent/inbox/spliced" and event.data["operation"] == "claim"
        for event in session.events
    )
    assert first not in {item.message_id for item in restored.pending()}


async def test_steer_is_applied_at_the_next_step_and_events_are_published() -> None:
    adapter = GatedAdapter()
    bus = LiveEventBus()
    observed: list[str] = []
    bus.subscribe("*", lambda event_type, data: observed.append(event_type))
    agent = Agent(adapter, event_bus=bus)

    submit = asyncio.create_task(agent.followup("initial"))
    await adapter.started.wait()
    assert agent.status == "running"
    await agent.steer("please correct this")
    adapter.release.set()
    await submit
    await agent.when_idle()

    assert len(adapter.requests) == 2
    assert adapter.requests[1].messages[-1]["content"] == "please correct this"
    assert "session/event" in observed
    assert "agent/status" in observed


async def test_inject_does_not_wake_idle_but_is_used_by_next_followup() -> None:
    adapter = GatedAdapter()
    adapter.release.set()
    agent = Agent(adapter)

    await agent.inject("静默上下文")
    await asyncio.sleep(0)
    assert agent.status == "idle"
    assert adapter.requests == []

    await agent.followup("真正任务")
    await agent.when_idle()

    assert adapter.requests[0].messages[-2]["content"] == "真正任务"
    assert adapter.requests[0].messages[-1]["content"] == "静默上下文"


async def test_concurrent_followups_share_one_driver() -> None:
    adapter = GatedAdapter()
    agent = Agent(adapter)

    first = asyncio.create_task(agent.followup("one"))
    second = asyncio.create_task(agent.followup("two"))
    await adapter.started.wait()
    assert adapter.max_active == 1
    adapter.release.set()
    await asyncio.gather(first, second)
    await agent.when_idle()

    assert adapter.max_active == 1
    assert len(adapter.requests) == 2


async def test_cancel_can_keep_inbox_messages() -> None:
    adapter = GatedAdapter()
    agent = Agent(adapter)

    await agent.followup("running")
    await adapter.started.wait()
    await agent.followup("preserve me")
    await agent.cancel(CancelCause(kind="user"), keep_inbox=True)
    await agent.when_idle()

    assert agent.status == "idle"
    assert [item.content for item in agent.inbox.pending("next_turn")] == ["preserve me"]
