"""阶段 2 的 Agent Handle：生命周期所有权、Inbox 和单 Driver。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from python_agent.approval.service import ApprovalService
from python_agent.config import AgentPreset
from python_agent.core.agent_loop import AgentLoop, ModelRequestStatus, RunResult
from python_agent.core.inbox import Inbox, UserMessage
from python_agent.core.lifecycle import AgentStatus, CancelCause
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import MessageId, SessionId
from python_agent.llm.adapter import ModelAdapter, ModelRouter
from python_agent.llm.retry import ModelRetryPolicy
from python_agent.session.events import SessionEvent
from python_agent.session.session import Session
from python_agent.tools.policies import ExecuteHandler, PostHandler, PreHandler
from python_agent.tools.registry import ToolRegistry


class Agent:
    """对外暴露的 Agent Handle。

    Agent 是 Driver Task、AgentLoop、Inbox 和事件订阅的唯一所有者。调用方只需要使用
    followup、steer、inject、cancel 和 when_idle，不需要直接管理 asyncio Task，也不能
    因为并发输入而创建第二个 Driver。
    """

    def __init__(
        self,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        session: Session | None = None,
        system_prompt: str | None = None,
        workspace: Path | None = None,
        event_bus: LiveEventBus | None = None,
        approval_service: ApprovalService | None = None,
        approval_required: set[str] | frozenset[str] | None = None,
        spill_directory: Path | None = None,
        pre_policies: tuple[PreHandler, ...] = (),
        execute_policies: tuple[ExecuteHandler, ...] = (),
        post_policies: tuple[PostHandler, ...] = (),
        request_retry_policy: ModelRetryPolicy | None = None,
        recover_orphaned_claims: bool = False,
    ) -> None:
        """创建一个长期存活的 Handle，并把所有 Driver 相关资源绑定到本实例。

        已传入 Session 时会先重放 Inbox 事件；新 Session 则从空队列开始。AgentLoop
        使用同一 Session，因此输入队列、模型消息和实时事件能够共享一个事实源。
        """

        self.config = config or AgentPreset()
        self.event_bus = event_bus or LiveEventBus()
        self.session = session or Session.new(
            agent_preset=self.config.id,
            cwd=workspace or self.config.workspace,
        )
        self.session.set_event_listener(self._on_session_event)
        self.inbox = Inbox(
            self.session,
            replay=session is not None,
            recover_orphaned_claims=recover_orphaned_claims,
        )
        self.loop = AgentLoop(
            adapter,
            tools,
            config=self.config,
            session=self.session,
            system_prompt=system_prompt,
            workspace=workspace,
            event_handler=self._on_loop_event,
            approval_service=approval_service,
            approval_required=approval_required,
            spill_directory=spill_directory,
            pre_policies=pre_policies,
            execute_policies=execute_policies,
            post_policies=post_policies,
            request_retry_policy=request_retry_policy,
        )
        self._status: AgentStatus = "idle"
        self._driver_task: asyncio.Task[None] | None = None
        self._driver_lock = asyncio.Lock()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._last_result: RunResult | None = None
        self._driver_error: Exception | None = None
        self._disposed = False

    @property
    def id(self) -> SessionId:
        """返回与 Session 相同的稳定 Agent ID。"""

        return self.session.id

    @property
    def status(self) -> AgentStatus:
        """返回公开生命周期状态，只暴露 idle 或 running。"""

        return self._status

    @property
    def last_result(self) -> RunResult | None:
        """返回最近一个完成 Turn 的结果。"""

        return self._last_result

    @property
    def active_request(self) -> ModelRequestStatus | None:
        """返回当前模型请求和实时等待时长，供 CLI 状态面板查询。"""

        return self.loop.active_request

    async def run(self, prompt: str) -> RunResult:
        """兼容阶段 1 的单次 API：提交 followup 并等待 Agent 重新 idle。"""

        await self.followup(prompt)
        await self.when_idle()
        if self._last_result is None:
            raise RuntimeError("agent became idle without a result")
        return self._last_result

    async def followup(self, message: str | UserMessage) -> MessageId:
        """把消息放入 next_turn，并在 Agent idle 时启动 Driver。"""

        self._ensure_not_disposed()
        message_id = self.inbox.append(message, "followup")
        await self._ensure_driver()
        return message_id

    async def steer(self, message: str | UserMessage) -> MessageId:
        """把纠偏消息放入 next_step，并唤醒 idle Agent。"""

        self._ensure_not_disposed()
        message_id = self.inbox.append(message, "steer")
        await self._ensure_driver()
        return message_id

    async def inject(self, message: str | UserMessage) -> MessageId:
        """把静默上下文放入 next_step，但不唤醒 idle Agent。"""

        self._ensure_not_disposed()
        return self.inbox.append(message, "inject")

    async def resume_pending(self) -> None:
        """恢复进程重启前仍在 Inbox 中的可唤醒工作。

        方法不会插入新消息，只复用单 Driver 的创建规则。仅有 inject 时继续保持 idle；
        存在 followup 或 steer 时才启动 Driver，避免恢复动作本身改变三种输入语义。
        """

        self._ensure_not_disposed()
        await self._ensure_driver()

    async def cancel(self, cause: CancelCause, *, keep_inbox: bool = False) -> None:
        """请求取消当前 Driver，并等待它和当前模型/工具调用收敛。

        keep_inbox=True 只取消当前执行，不删除尚未领取的消息；False 会通过 Inbox.delete
        事件清空两个队列。取消后 Agent 保持 idle，下一次输入可以重新唤醒 Driver。
        """

        self._ensure_not_disposed(allow_disposed=True)
        if not keep_inbox:
            self.inbox.clear()
        self.loop.cancel()
        task = self._driver_task
        if task is not None and not task.done():
            # 先设置协作式信号，再取消 Driver 的 await，让 when_idle 可以尽快收敛。
            task.cancel()
            await asyncio.shield(task)
        await self.event_bus.emit(
            "agent/status",
            {
                "agent_id": str(self.id),
                "status": self._status,
                "cancel": cause.model_dump(mode="json"),
            },
        )

    async def when_idle(self) -> None:
        """等待当前 Driver 以及收敛前创建的替代 Driver 全部结束。"""

        while True:
            await self._idle_event.wait()
            async with self._driver_lock:
                task = self._driver_task
                # Driver 已完成就代表当前执行已经收敛；即使 keep_inbox 保留了待处理消息，
                # 也不能让 when_idle 永远等待。后续 followup/steer 会重新创建 Driver。
                if task is None or task.done():
                    error = self._driver_error
                    self._driver_error = None
                    if error is not None:
                        raise error
                    return
                self._idle_event.clear()

    async def dispose(self) -> None:
        """按生命周期顺序取消 Driver、清空 Inbox，并释放 Agent Handle。"""

        if self._disposed:
            return
        await self.cancel(CancelCause(kind="disposed"), keep_inbox=False)
        self._disposed = True
        await self.event_bus.emit(
            "agent/status",
            {"agent_id": str(self.id), "status": "idle", "disposed": True},
        )

    async def _ensure_driver(self) -> None:
        """在锁保护下检查并创建唯一 Driver；已有 Driver 时只让消息留在 Inbox。"""

        async with self._driver_lock:
            if self._disposed or not self.inbox.has_wakeup_pending:
                return
            if self._driver_task is None or self._driver_task.done():
                self.loop.reset_cancel()
                self._driver_error = None
                self._idle_event.clear()
                self._driver_task = asyncio.create_task(
                    self._drive(),
                    name=f"agent-driver-{self.id}",
                )

    async def _drive(self) -> None:
        """单一 Driver 主循环：一个 Turn 结束后继续处理队列中的下一个唤醒消息。"""

        self._status = "running"
        await self.event_bus.emit(
            "agent/status",
            {"agent_id": str(self.id), "status": self._status},
        )
        try:
            while self.inbox.has_wakeup_pending:
                wake_message = self.inbox.claim_idle_wakeup()
                if wake_message is None:
                    break
                self.loop.reset_cancel()
                self._last_result = await self.loop.run_turn(
                    # 传入完整消息而不是只有 content，确保 user/message 沿用 Inbox MessageId；
                    # 该关联是阶段四判断孤立 claim、避免崩溃丢任务的依据。
                    wake_message,
                    step_input_provider=self.inbox.claim_next_step,
                )
        except asyncio.CancelledError:
            # cancel() 已经设置取消信号；这里吞掉 Task 取消，让 Driver 正常回到 idle。
            self._driver_error = None
        except Exception as exc:
            self._driver_error = exc
            await self.event_bus.emit(
                "agent/error",
                {
                    "agent_id": str(self.id),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        finally:
            self._status = "idle"
            await self.event_bus.emit(
                "agent/status",
                {"agent_id": str(self.id), "status": self._status},
            )
            # 必须在最后一次 await 之后再唤醒 when_idle，确保观察者完成后 Driver Task
            # 已经真正返回，避免 when_idle 误判为未收敛并错过下一次唤醒。
            self._idle_event.set()

    async def _on_loop_event(self, event_type: str, data: dict[str, Any]) -> None:
        """转发模型增量、工具调用和工具结果等实时事实。"""

        await self.event_bus.emit(event_type, data)
        if event_type == "tool/result":
            await self.event_bus.emit("tools/result", data)

    def _on_session_event(self, event: SessionEvent) -> None:
        """在事件追加成功后同步通知 Session 观察者，不创建后台 Task。"""

        self.event_bus.emit_sync(
            "session/event",
            {"session_id": str(self.id), "event": event.model_dump(mode="json")},
        )

    def _ensure_not_disposed(self, *, allow_disposed: bool = False) -> None:
        """阻止已释放 Handle 接收新工作；cancel 的重复收敛可以被显式允许。"""

        if self._disposed and not allow_disposed:
            raise RuntimeError(f"agent {self.id} is disposed")


AgentHandle = Agent

__all__ = ["Agent", "AgentHandle", "RunResult"]
