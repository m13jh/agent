"""手工验证阶段 2 Agent Handle 的脚本。

该脚本不依赖 pytest：它通过受控模型暂停和释放，人工观察 inject、steer、并发 followup、
取消以及 Live Event Bus 的实际运行顺序。
"""

import asyncio

from python_agent import AgentManager
from python_agent.core.lifecycle import CancelCause
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.llm.types import AssistantResponse, ModelRequest


class ManualAdapter:
    """可手动控制暂停和释放的模型，用于观察 Agent 生命周期。"""

    name = "manual"

    def __init__(self):
        """准备请求记录、模型开始信号和外部释放信号。"""

        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """阻塞到 release，模拟模型还未结束时用户插入控制消息。"""

        self.requests.append(request)
        self.started.set()
        self.active += 1
        self.max_active = max(self.max_active, self.active)

        try:
            await self.release.wait()
            return AssistantResponse(content=f"模型完成第 {len(self.requests)} 次请求")
        finally:
            self.active -= 1


async def main():
    """依次执行阶段 2的五个手工检查并打印观察结果。"""

    event_types = []
    bus = LiveEventBus()

    bus.subscribe(
        "*",
        lambda event_type, data: event_types.append(event_type),
    )

    manager = AgentManager(event_bus=bus)

    # 1. 测试 inject：idle 状态下不应该触发模型
    adapter = ManualAdapter()
    adapter.release.set()
    agent = await manager.create(adapter)

    await agent.inject("这是静默上下文")
    await asyncio.sleep(0)

    print("inject 后状态：", agent.status)
    print("inject 后模型请求数：", len(adapter.requests))

    assert agent.status == "idle"
    assert len(adapter.requests) == 0

    # followup 会唤醒 Agent，并且把之前的 inject 一起带入模型上下文
    await agent.followup("真正的任务")
    await agent.when_idle()

    print(
        "第一次请求中的消息：",
        [message["content"] for message in adapter.requests[0].messages],
    )

    # 2. 测试 steer：运行中的 Agent 在下一 Step 接收纠偏消息
    adapter = ManualAdapter()
    agent = await manager.create(adapter)

    running_task = asyncio.create_task(agent.followup("原始任务"))

    await adapter.started.wait()

    print("模型运行中状态：", agent.status)

    await agent.steer("请修正方向")

    adapter.release.set()

    await running_task
    await agent.when_idle()

    print("steer 后模型请求数：", len(adapter.requests))
    print(
        "第二次请求最后一条消息：",
        adapter.requests[1].messages[-1]["content"],
    )

    assert len(adapter.requests) == 2
    assert adapter.requests[1].messages[-1]["content"] == "请修正方向"

    # 3. 测试并发 followup：只能有一个 Driver
    adapter = ManualAdapter()
    agent = await manager.create(adapter)

    first = asyncio.create_task(agent.followup("任务一"))
    second = asyncio.create_task(agent.followup("任务二"))

    await adapter.started.wait()

    print("并发执行时最大模型并发数：", adapter.max_active)

    assert adapter.max_active == 1

    adapter.release.set()

    await asyncio.gather(first, second)
    await agent.when_idle()

    print("并发 followup 的模型请求数：", len(adapter.requests))

    assert len(adapter.requests) == 2
    assert adapter.max_active == 1

    # 4. 测试 cancel(..., keep_inbox=True)
    adapter = ManualAdapter()
    agent = await manager.create(adapter)

    await agent.followup("正在运行的任务")
    await adapter.started.wait()

    await agent.followup("取消后保留的任务")

    await agent.cancel(
        CancelCause(kind="user", message="用户手动取消"),
        keep_inbox=True,
    )
    await agent.when_idle()

    pending = [item.content for item in agent.inbox.pending("next_turn")]

    print("取消后的状态：", agent.status)
    print("保留的 Inbox 消息：", pending)

    assert agent.status == "idle"
    assert pending == ["取消后保留的任务"]

    # 5. 检查 Live Event Bus
    print("是否收到 agent/status：", "agent/status" in event_types)
    print("是否收到 session/event：", "session/event" in event_types)

    assert "agent/status" in event_types
    assert "session/event" in event_types

    await manager.shutdown()
    print("阶段 2 手动测试全部通过")


asyncio.run(main())
