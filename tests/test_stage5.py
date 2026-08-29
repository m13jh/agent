"""阶段五工具并发、资源预算与模型请求重试测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from python_agent.config import AgentPreset
from python_agent.core.agent_loop import AgentLoop
from python_agent.errors import ModelError
from python_agent.ids import SessionId
from python_agent.llm.fake_adapter import FakeAdapter
from python_agent.llm.retry import ModelRetryContext, RetryDecision
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall
from python_agent.tools.definition import FunctionTool
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.runtime import ToolRuntime
from python_agent.tools.types import ToolContext


def _context(tmp_path: Path) -> ToolContext:
    """创建阶段五 Runtime 测试共用的只读上下文。"""

    return ToolContext(session_id=SessionId("stage5"), workspace=tmp_path)


async def test_parallel_tools_overlap_but_results_keep_model_order(tmp_path: Path) -> None:
    """验证并发安全工具实际重叠完成，返回值仍按模型调用顺序排列。"""

    both_started = asyncio.Event()
    active = 0
    max_active = 0
    completion_order: list[str] = []

    async def body(arguments: dict, context: ToolContext) -> str:
        """让第二个调用先完成，稳定制造完成顺序与模型顺序相反的场景。"""

        nonlocal active, max_active
        del context
        name = arguments["name"]
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await both_started.wait()
        if name == "first":
            await asyncio.sleep(0.02)
        completion_order.append(name)
        active -= 1
        return name

    tool = FunctionTool(
        name="parallel",
        description="parallel test",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        body=body,
        concurrency_safe=True,
    )
    calls = [
        ToolCall(id="first-call", name="parallel", arguments={"name": "first"}),
        ToolCall(id="second-call", name="parallel", arguments={"name": "second"}),
    ]

    results = await ToolRuntime(
        ToolRegistry([tool]),
        max_parallel_tools=2,
    ).execute_many(calls, _context(tmp_path))

    assert max_active == 2
    assert completion_order == ["second", "first"]
    assert [str(result.call_id) for result in results] == ["first-call", "second-call"]
    assert [result.content for result in results] == ["first", "second"]


async def test_parallel_pool_is_bounded(tmp_path: Path) -> None:
    """验证滚动池可以启动后续任务，但活动数量永远不超过配置上限。"""

    active = 0
    max_active = 0

    async def body(arguments: dict, context: ToolContext) -> int:
        """短暂占用一个槽位并返回调用序号。"""

        nonlocal active, max_active
        del context
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return arguments["index"]

    tool = FunctionTool(
        name="bounded",
        description="bounded pool test",
        parameters={"type": "object"},
        body=body,
        concurrency_safe=True,
    )
    calls = [
        ToolCall(id=f"call-{index}", name="bounded", arguments={"index": index})
        for index in range(6)
    ]
    results = await ToolRuntime(
        ToolRegistry([tool]),
        max_parallel_tools=2,
    ).execute_many(calls, _context(tmp_path))

    assert max_active == 2
    assert [result.content for result in results] == list(range(6))


async def test_exclusive_tool_forms_barrier_between_parallel_batches(tmp_path: Path) -> None:
    """验证写工具开始前读批次已排空，写完成后才启动下一批读。"""

    active_reads = 0
    write_active = False
    timeline: list[str] = []

    async def read_body(arguments: dict, context: ToolContext) -> str:
        """记录读工具开始/结束，并断言没有与 exclusive 写工具重叠。"""

        nonlocal active_reads
        del context
        name = arguments["name"]
        assert write_active is False
        active_reads += 1
        timeline.append(f"{name}:start")
        await asyncio.sleep(0.01)
        timeline.append(f"{name}:end")
        active_reads -= 1
        return name

    async def write_body(arguments: dict, context: ToolContext) -> str:
        """exclusive 工具执行时前一读批次必须已经全部结束。"""

        nonlocal write_active
        del arguments, context
        assert active_reads == 0
        write_active = True
        timeline.append("write:start")
        await asyncio.sleep(0.01)
        timeline.append("write:end")
        write_active = False
        return "write"

    read_tool = FunctionTool(
        name="read",
        description="parallel read",
        parameters={"type": "object"},
        body=read_body,
        concurrency_safe=True,
    )
    write_tool = FunctionTool(
        name="write",
        description="exclusive write",
        parameters={"type": "object"},
        body=write_body,
        concurrency_safe=False,
    )
    calls = [
        ToolCall(id="r1", name="read", arguments={"name": "r1"}),
        ToolCall(id="r2", name="read", arguments={"name": "r2"}),
        ToolCall(id="w", name="write", arguments={}),
        ToolCall(id="r3", name="read", arguments={"name": "r3"}),
    ]

    await ToolRuntime(
        ToolRegistry([read_tool, write_tool]),
        max_parallel_tools=2,
    ).execute_many(calls, _context(tmp_path))

    write_start = timeline.index("write:start")
    write_end = timeline.index("write:end")
    assert timeline.index("r1:end") < write_start
    assert timeline.index("r2:end") < write_start
    assert write_end < timeline.index("r3:start")


async def test_parallel_group_cancellation_returns_one_result_per_call(tmp_path: Path) -> None:
    """验证 Driver 取消会排空所有工具 Task，并为每个模型 call 返回明确错误。"""

    started = asyncio.Event()

    async def body(arguments: dict, context: ToolContext) -> str:
        """无限等待，直到外部取消调度 Task。"""

        del arguments, context
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    tool = FunctionTool(
        name="cancelled",
        description="cancellation test",
        parameters={"type": "object"},
        body=body,
        concurrency_safe=True,
    )
    calls = [ToolCall(id=f"c{index}", name="cancelled", arguments={}) for index in range(3)]
    context = _context(tmp_path)
    task = asyncio.create_task(
        ToolRuntime(ToolRegistry([tool]), max_parallel_tools=1).execute_many(calls, context)
    )
    await started.wait()
    context.cancel_event.set()
    task.cancel()
    results = await task

    assert context.cancel_event.is_set()
    assert [str(result.call_id) for result in results] == ["c0", "c1", "c2"]
    assert all(result.is_error for result in results)


async def test_agent_loop_cancellation_still_pairs_every_tool_call(tmp_path: Path) -> None:
    """验证 Agent Loop 取消后仍持久化全部 call/result，再以 aborted 结束生命周期。"""

    started = asyncio.Event()

    async def body(arguments: dict, context: ToolContext) -> str:
        """阻塞第一个工具，给测试留下取消 Driver 的稳定窗口。"""

        del arguments, context
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    tool = FunctionTool(
        name="blocking",
        description="agent cancellation test",
        parameters={"type": "object"},
        body=body,
        concurrency_safe=True,
    )
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [
                    {"id": f"blocking-{index}", "name": "blocking", "arguments": {}}
                    for index in range(3)
                ],
                "finish_reason": "tool_calls",
            }
        ]
    )
    loop = AgentLoop(
        adapter,
        ToolRegistry([tool]),
        config=AgentPreset(workspace=tmp_path, max_parallel_tools=1),
    )
    task = asyncio.create_task(loop.run("取消工具组"))
    await started.wait()
    loop.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    call_events = [event for event in loop.session.events if event.type == "tool/call"]
    result_events = [event for event in loop.session.events if event.type == "tool/result"]
    assert len(call_events) == len(result_events) == 3
    assert [event.data["call_id"] for event in result_events] == [
        "blocking-0",
        "blocking-1",
        "blocking-2",
    ]
    assert all(event.data["is_error"] for event in result_events)
    assert [event for event in loop.session.events if event.type == "turn/end"][-1].data[
        "reason"
    ] == "aborted"


async def test_agent_loop_commits_parallel_results_in_model_order() -> None:
    """验证工具乱序完成时，Session 的 tool/result 仍保持模型 call 顺序。"""

    async def body(arguments: dict, context: ToolContext) -> str:
        """让第一个调用比第二个调用更晚结束。"""

        del context
        if arguments["value"] == "first":
            await asyncio.sleep(0.02)
        return arguments["value"]

    tool = FunctionTool(
        name="parallel",
        description="agent order test",
        parameters={"type": "object"},
        body=body,
        concurrency_safe=True,
    )
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [
                    {"id": "first", "name": "parallel", "arguments": {"value": "first"}},
                    {"id": "second", "name": "parallel", "arguments": {"value": "second"}},
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "完成", "finish_reason": "stop"},
        ]
    )
    result = await AgentLoop(
        adapter,
        ToolRegistry([tool]),
        config=AgentPreset(max_parallel_tools=2),
    ).run("执行两个调用")

    result_events = [event for event in result.session.events if event.type == "tool/result"]
    assert [event.data["call_id"] for event in result_events] == ["first", "second"]


async def test_token_budget_skips_tools_and_concludes_turn() -> None:
    """验证模型响应耗尽 Turn Token 预算后，工具只记录错误结果而不执行副作用。"""

    called = False

    async def body(arguments: dict, context: ToolContext) -> str:
        """若预算保护正确，此函数永远不会执行。"""

        nonlocal called
        del arguments, context
        called = True
        return "unexpected"

    tool = FunctionTool(
        name="side_effect",
        description="must be skipped",
        parameters={"type": "object"},
        body=body,
    )
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "effect", "name": "side_effect", "arguments": {}}],
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            }
        ]
    )
    result = await AgentLoop(
        adapter,
        ToolRegistry([tool]),
        config=AgentPreset(max_turn_tokens=100),
    ).run("不要超预算")

    assert called is False
    assert adapter.requests[0].max_tokens == 100
    assert result.finish_reason == "token_budget"
    tool_result = next(event for event in result.session.events if event.type == "tool/result")
    assert tool_result.data["is_error"] is True
    assert "Token 预算" in tool_result.data["content"]
    turn_end = [event for event in result.session.events if event.type == "turn/end"][-1]
    assert turn_end.data["reason"] == "token_budget"
    assert turn_end.data["budget"]["total_tokens"] == 100


async def test_provider_cost_budget_is_enforced() -> None:
    """验证 Provider usage 中的费用跨请求累计并进入 turn/end 快照。"""

    adapter = FakeAdapter(
        [
            {
                "content": "已经产生回答",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost_usd": 0.02,
                },
            }
        ]
    )
    result = await AgentLoop(
        adapter,
        config=AgentPreset(max_turn_cost_usd=0.01),
    ).run("费用测试")

    assert result.answer == "已经产生回答"
    assert result.finish_reason == "cost_budget"
    turn_end = [event for event in result.session.events if event.type == "turn/end"][-1]
    assert turn_end.data["budget"]["cost_known"] is True
    assert turn_end.data["budget"]["cost_usd"] == pytest.approx(0.02)


async def test_configured_token_prices_can_estimate_cost_budget() -> None:
    """验证 Provider 不返回费用时，可以使用 preset 的每百万 Token 单价估算。"""

    adapter = FakeAdapter(
        [
            {
                "content": "价格估算",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                },
            }
        ]
    )
    result = await AgentLoop(
        adapter,
        config=AgentPreset(
            max_turn_cost_usd=0.01,
            input_cost_per_million_tokens=10.0,
            output_cost_per_million_tokens=20.0,
        ),
    ).run("估算费用")

    assert result.finish_reason == "cost_budget"
    turn_end = [event for event in result.session.events if event.type == "turn/end"][-1]
    assert turn_end.data["budget"]["cost_usd"] == pytest.approx(0.02)


class SlowAdapter:
    """超过墙钟预算的模型适配器。"""

    name = "slow"

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """故意等待，触发 AgentLoop 的 Turn deadline。"""

        del request, cancel_event
        await asyncio.sleep(0.05)
        return AssistantResponse(content="too late")


async def test_wall_time_budget_cancels_slow_model_cleanly() -> None:
    """验证墙钟超时以 wall_time 正常结束 Turn，而不是泄漏 TimeoutError。"""

    result = await AgentLoop(
        SlowAdapter(),
        config=AgentPreset(max_turn_seconds=0.005, model_max_retries=0),
    ).run("墙钟测试")

    assert result.finish_reason == "wall_time"
    assert result.answer == ""
    assert [event.type for event in result.session.events].count("request/error") == 1
    assert [event for event in result.session.events if event.type == "step/end"][-1].data[
        "reason"
    ] == "wall_time"


async def test_wall_time_budget_cancels_tool_group_without_poisoning_agent() -> None:
    """验证预算取消工具 Task 后不会把长期 cancel_event 误标记为用户取消。"""

    async def body(arguments: dict, context: ToolContext) -> str:
        """故意超过 Turn 剩余时间。"""

        del arguments, context
        await asyncio.sleep(0.05)
        return "too late"

    tool = FunctionTool(
        name="slow_tool",
        description="wall-time tool test",
        parameters={"type": "object"},
        body=body,
        concurrency_safe=True,
    )
    adapter = FakeAdapter(
        [
            {
                "tool_calls": [{"id": "slow", "name": "slow_tool", "arguments": {}}],
                "finish_reason": "tool_calls",
            }
        ]
    )
    loop = AgentLoop(
        adapter,
        ToolRegistry([tool]),
        config=AgentPreset(max_turn_seconds=0.005, max_parallel_tools=1),
    )
    result = await loop.run("工具墙钟测试")

    assert result.finish_reason == "wall_time"
    assert loop.cancel_event.is_set() is False
    call_events = [event for event in result.session.events if event.type == "tool/call"]
    result_events = [event for event in result.session.events if event.type == "tool/result"]
    assert len(call_events) == len(result_events) == 1
    assert result_events[0].data["is_error"] is True


class FlakyAdapter:
    """前若干次抛 ModelError，之后成功的可控适配器。"""

    name = "flaky"

    def __init__(self, failures: int, *, error: str = "network temporarily failed") -> None:
        """设置失败次数和错误文本。"""

        self.failures = failures
        self.error = error
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """按调用次数决定失败或成功。"""

        del cancel_event
        self.requests.append(request)
        if len(self.requests) <= self.failures:
            raise ModelError(self.error)
        return AssistantResponse(content="重试成功")


async def test_model_request_retries_then_succeeds_without_duplicate_user_message() -> None:
    """验证两次有限重试后成功，并且重试不会复制模型历史中的用户消息。"""

    adapter = FlakyAdapter(2)
    live_events: list[tuple[str, dict]] = []
    result = await AgentLoop(
        adapter,
        config=AgentPreset(model_max_retries=2, model_retry_base_delay_seconds=0),
        event_handler=lambda kind, data: live_events.append((kind, data)),
    ).run("重试任务")

    assert result.answer == "重试成功"
    assert len(adapter.requests) == 3
    assert [event.type for event in result.session.events].count("request/error") == 2
    assert [event.type for event in result.session.events].count("request/retry") == 2
    assert [event.type for event in result.session.events].count("user/message") == 1
    starts = [data for kind, data in live_events if kind == "model/request_start"]
    assert [data["attempt"] for data in starts] == [1, 2, 3]


async def test_permanent_model_error_is_not_retried() -> None:
    """验证认证类永久错误即使配置多次重试也会立即失败。"""

    adapter = FlakyAdapter(10, error="authentication failed: invalid API key")
    loop = AgentLoop(
        adapter,
        config=AgentPreset(model_max_retries=5, model_retry_base_delay_seconds=0),
    )

    with pytest.raises(ModelError, match="authentication failed"):
        await loop.run("永久错误")

    assert len(adapter.requests) == 1
    error_event = next(event for event in loop.session.events if event.type == "request/error")
    assert error_event.data["will_retry"] is False


class RetryValueErrorOnce:
    """演示应用层可以替换默认策略，允许重试非 ModelError。"""

    async def decide(self, context: ModelRetryContext) -> RetryDecision:
        """只允许第一次 ValueError 重试。"""

        return RetryDecision(
            retry=isinstance(context.error, ValueError) and context.attempt == 1,
            reason="测试自定义策略",
        )


class ValueErrorAdapter:
    """第一次抛 ValueError、第二次成功的模型适配器。"""

    name = "value-error"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event,
    ) -> AssistantResponse:
        """按次数返回错误或答案。"""

        del request, cancel_event
        self.calls += 1
        if self.calls == 1:
            raise ValueError("custom retry")
        return AssistantResponse(content="custom success")


async def test_custom_retry_policy_can_replace_default_decision() -> None:
    """验证 request_retry_policy 确实是可注入扩展点。"""

    adapter = ValueErrorAdapter()
    result = await AgentLoop(
        adapter,
        config=AgentPreset(model_max_retries=1),
        request_retry_policy=RetryValueErrorOnce(),
    ).run("自定义重试")

    assert result.answer == "custom success"
    assert adapter.calls == 2
