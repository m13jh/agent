"""进程内子 Agent 的创建、鉴权、通知与 child-first 生命周期管理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.core.lifecycle import CancelCause
from python_agent.errors import SubagentLimitError, SubagentPermissionError
from python_agent.ids import MessageId, SessionId
from python_agent.llm.adapter import ModelRouter
from python_agent.session.events import utc_now
from python_agent.subagents.types import (
    SUBAGENT_TOOL_NAMES,
    SubagentInfo,
    SubagentSettled,
    SubagentSpec,
)

if TYPE_CHECKING:
    from python_agent.core.agent_manager import AgentManager


@dataclass(slots=True)
class _SubagentRecord:
    """SubagentManager 拥有的运行期记录；不会直接写入模型上下文。"""

    parent_id: SessionId
    child: Agent
    description: str
    created_at: datetime = field(default_factory=utc_now)
    submitted_generation: int = 0
    notified_generation: int = 0
    work_event: asyncio.Event = field(default_factory=asyncio.Event)
    watcher: asyncio.Task[None] | None = None
    interrupted: bool = False
    disposed: bool = False
    last_answer: str | None = None
    finish_reason: str | None = None


class SubagentManager:
    """由 AgentManager 持有的进程内子 Agent 编排器。"""

    def __init__(self, agent_manager: AgentManager) -> None:
        """绑定唯一的 AgentManager，并创建父子索引。"""

        self.agent_manager = agent_manager
        self._records: dict[SessionId, _SubagentRecord] = {}
        self._children: dict[SessionId, list[SessionId]] = {}

    def enable_for(self, agent: Agent) -> None:
        """按 Agent 配置注册与当前父级绑定的子 Agent 管理工具。

        每个工具实例都绑定具体 parent Agent，不能从父 Registry 直接复制给孩子。达到最大
        深度时不注册 spawn_agent，但仍可保留 list/followup/interrupt 来管理已有直接孩子。
        """

        if not agent.config.subagents_enabled:
            return
        if agent.config.tools:
            allowed = set(agent.config.tools) & set(SUBAGENT_TOOL_NAMES)
        else:
            allowed = set(SUBAGENT_TOOL_NAMES)
        if agent.session.header.delegation_depth >= agent.config.max_delegation_depth:
            allowed.discard("spawn_agent")

        # 局部导入避免 tool.py 为类型提示反向导入本模块时形成初始化环。
        from python_agent.subagents.tool import management_tools

        for tool in management_tools(self, agent):
            if tool.name in allowed and agent.tools.maybe_get(tool.name) is None:
                agent.tools.register(tool)

    @staticmethod
    def _authorized_tools(parent: Agent) -> set[str]:
        """计算父 Agent 实际可调用的工具名称，而不是原始 Registry 的潜在能力。"""

        if parent.config.tools:
            return set(parent.config.tools)
        return set(parent.tools.names())

    @staticmethod
    def _child_preset_id(
        parent: Agent,
        spec: SubagentSpec,
        allowed_tools: set[str],
        depth: int,
    ) -> str:
        """根据父 preset 和全部子能力生成稳定、紧凑的 preset ID。"""

        payload = {
            "parent": parent.config.id,
            "depth": depth,
            "provider": spec.provider or parent.config.provider,
            "model": spec.model or parent.config.model,
            "persona": spec.persona,
            "tools": sorted(allowed_tools),
            "max_steps": spec.max_steps or parent.config.max_steps,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"{parent.config.id}/subagent-{depth}-{digest}"

    def _derive_child_config(
        self,
        parent: Agent,
        spec: SubagentSpec,
        allowed_tools: set[str],
        depth: int,
    ) -> AgentPreset:
        """继承父配置并只允许显式收窄步数、工具或更换已可路由模型。"""

        requested_steps = spec.max_steps or parent.config.max_steps
        if requested_steps > parent.config.max_steps:
            raise SubagentLimitError(
                f"child max_steps {requested_steps} exceeds parent limit {parent.config.max_steps}"
            )
        provider = spec.provider or parent.config.provider
        if provider != parent.config.provider and not isinstance(parent.loop.adapter, ModelRouter):
            raise SubagentPermissionError(
                "child cannot change provider when parent uses a direct ModelAdapter"
            )
        return parent.config.model_copy(
            update={
                "id": self._child_preset_id(parent, spec, allowed_tools, depth),
                "provider": provider,
                "model": spec.model or parent.config.model,
                "max_steps": requested_steps,
                "workspace": parent.session.header.cwd,
                "tools": tuple(sorted(allowed_tools)),
                "subagents_enabled": bool(allowed_tools & set(SUBAGENT_TOOL_NAMES)),
            }
        )

    async def start(self, parent: Agent, prompt: str, spec: SubagentSpec) -> SessionId:
        """创建独立 child Session、提交首个 Turn，并立即返回稳定 SessionId。"""

        if not prompt.strip():
            raise ValueError("subagent prompt must be non-empty")
        if not parent.config.subagents_enabled:
            raise SubagentPermissionError(f"subagents are disabled for parent {parent.id}")
        depth = parent.session.header.delegation_depth + 1
        if depth > parent.config.max_delegation_depth:
            raise SubagentLimitError(
                f"delegation depth {depth} exceeds max {parent.config.max_delegation_depth}"
            )
        direct_children = self._children.get(parent.id, [])
        if len(direct_children) >= parent.config.max_subagents:
            raise SubagentLimitError(
                f"parent {parent.id} already owns {len(direct_children)} subagents"
            )

        parent_tools = self._authorized_tools(parent)
        requested_tools = (
            set(spec.allowed_tools) if spec.allowed_tools is not None else parent_tools
        )
        unauthorized = requested_tools - parent_tools
        if unauthorized:
            raise SubagentPermissionError(
                "child tools must be a subset of parent tools: " + ", ".join(sorted(unauthorized))
            )
        if depth >= parent.config.max_delegation_depth:
            # 处在最大允许深度的 child 仍可管理自己的既有直接孩子，但不能再看到 spawn。
            requested_tools.discard("spawn_agent")

        # 管理工具含 parent 绑定状态，不能共享实例；孩子创建后由 enable_for 重新绑定。
        base_tool_names = requested_tools - set(SUBAGENT_TOOL_NAMES)
        child_tools = parent.tools.subset(base_tool_names)
        child_config = self._derive_child_config(parent, spec, requested_tools, depth)
        system_prompt = parent.loop.system_prompt
        if spec.persona:
            system_prompt += (
                "\n\nYou are a delegated subagent. Follow this additional persona/instruction:\n"
                + spec.persona
            )

        child = await self.agent_manager.create(
            parent.loop.adapter,
            child_tools,
            config=child_config,
            system_prompt=system_prompt,
            workspace=parent.session.header.cwd,
            parent_session_id=parent.id,
            origin="subagent",
            delegation_depth=depth,
        )
        record = _SubagentRecord(parent_id=parent.id, child=child, description=spec.description)
        self._records[child.id] = record
        self._children.setdefault(parent.id, []).append(child.id)
        record.watcher = asyncio.create_task(
            self._watch_child(record),
            name=f"subagent-watcher-{child.id}",
        )
        await self.agent_manager.event_bus.emit(
            "subagent/created",
            {
                "parent_id": str(parent.id),
                "child_id": str(child.id),
                "delegation_depth": depth,
                "description": spec.description,
                "allowed_tools": sorted(requested_tools),
            },
        )
        try:
            await self._submit(record, prompt)
        except Exception:
            await self._dispose_record(record)
            raise
        return child.id

    def _owned_record(self, parent: Agent, child_id: SessionId) -> _SubagentRecord:
        """验证 parent 是 child 的直接父级，并返回运行记录。"""

        record = self._records.get(child_id)
        if record is None:
            raise KeyError(f"subagent not found: {child_id}")
        if record.parent_id != parent.id:
            raise SubagentPermissionError(
                f"agent {parent.id} is not the direct parent of subagent {child_id}"
            )
        return record

    async def _submit(self, record: _SubagentRecord, prompt: str) -> MessageId:
        """提交工作并唤醒长期 watcher；所有 followup 复用 child 自己的 Inbox。"""

        message_id = await record.child.followup(prompt)
        record.submitted_generation += 1
        record.interrupted = False
        record.work_event.set()
        return message_id

    async def followup(self, parent: Agent, child_id: SessionId, prompt: str) -> MessageId:
        """只允许直接父级向现有 child 提交新的独立 Turn。"""

        if not prompt.strip():
            raise ValueError("subagent followup must be non-empty")
        return await self._submit(self._owned_record(parent, child_id), prompt)

    async def interrupt(self, parent: Agent, child_id: SessionId) -> None:
        """只允许直接父级取消 child 当前执行并清空其待处理 Inbox。"""

        record = self._owned_record(parent, child_id)
        record.interrupted = True
        await record.child.cancel(
            CancelCause(kind="parent", message=f"interrupted by parent {parent.id}"),
            keep_inbox=False,
        )

    async def wait(self, parent: Agent, child_id: SessionId) -> SubagentSettled:
        """等待直接 child 当前批次收敛，并返回最新结果。"""

        record = self._owned_record(parent, child_id)
        await record.child.when_idle()
        result = record.child.last_result
        settled = SubagentSettled(
            child_id=record.child.id,
            parent_id=record.parent_id,
            answer=result.answer if result is not None else "",
            finish_reason=(
                "interrupted"
                if record.interrupted
                else result.finish_reason
                if result is not None
                else "idle"
            ),
        )
        # wait 是公开同步边界，即使 watcher 尚未获得调度，也应让 list_children 立刻看到结果。
        record.last_answer = settled.answer
        record.finish_reason = settled.finish_reason
        return settled

    async def list_children(self, parent_id: SessionId) -> list[SubagentInfo]:
        """返回指定父级的直接孩子快照，不递归混入后代。"""

        infos: list[SubagentInfo] = []
        for child_id in self._children.get(parent_id, []):
            record = self._records.get(child_id)
            if record is not None:
                infos.append(self._info(record))
        return infos

    @staticmethod
    def _info(record: _SubagentRecord) -> SubagentInfo:
        """把内部可变记录转换为不可变公开快照。"""

        return SubagentInfo(
            id=record.child.id,
            parent_id=record.parent_id,
            description=record.description,
            delegation_depth=record.child.session.header.delegation_depth,
            status=record.child.status,
            created_at=record.created_at,
            last_answer=record.last_answer,
            finish_reason=record.finish_reason,
        )

    async def _watch_child(self, record: _SubagentRecord) -> None:
        """长期拥有 child 的 settle 观察任务，并把结果耐久注入父 Agent Inbox。"""

        try:
            while not record.disposed:
                await record.work_event.wait()
                record.work_event.clear()
                # followup 可能在 when_idle 刚返回的边界进入。连续观察到 generation 稳定，
                # 才把当前 last_result 当成这一批工作的最终结果，避免漏掉新 Driver。
                while True:
                    generation = record.submitted_generation
                    await record.child.when_idle()
                    await asyncio.sleep(0)
                    if record.submitted_generation == generation:
                        break
                result = record.child.last_result
                answer = result.answer if result is not None else ""
                reason = (
                    "interrupted"
                    if record.interrupted
                    else result.finish_reason
                    if result is not None
                    else "idle"
                )
                record.last_answer = answer
                record.finish_reason = reason
                record.notified_generation = generation
                settled = SubagentSettled(
                    child_id=record.child.id,
                    parent_id=record.parent_id,
                    answer=answer,
                    finish_reason=reason,
                )
                await self.agent_manager.event_bus.emit(
                    "subagent/settled",
                    settled.model_dump(mode="json"),
                )
                parent = self.agent_manager.maybe_get(record.parent_id)
                if parent is not None:
                    try:
                        await parent.inject(
                            f"[subagent/result child_id={record.child.id} "
                            f"finish_reason={reason}]\n{answer}"
                        )
                    except RuntimeError:
                        pass
                # followup 可能在通知过程中进入；重新置位可确保下一批不会错过 watcher。
                if record.submitted_generation > generation:
                    record.work_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.agent_manager.event_bus.emit(
                "subagent/error",
                {
                    "parent_id": str(record.parent_id),
                    "child_id": str(record.child.id),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )

    async def dispose_descendants(self, parent_id: SessionId) -> None:
        """按 child-first 顺序取消并释放 parent 的所有直接/间接后代。"""

        for child_id in list(self._children.get(parent_id, [])):
            await self.dispose_descendants(child_id)
            record = self._records.get(child_id)
            if record is not None:
                await self._dispose_record(record)
        self._children.pop(parent_id, None)

    async def _dispose_record(self, record: _SubagentRecord) -> None:
        """释放 watcher 和 child Handle，并从父子索引删除记录。"""

        if record.disposed:
            return
        record.disposed = True
        if record.watcher is not None and record.watcher is not asyncio.current_task():
            record.watcher.cancel()
            await asyncio.gather(record.watcher, return_exceptions=True)
        await self.agent_manager._dispose_agent_only(record.child.id)
        self._records.pop(record.child.id, None)
        siblings = self._children.get(record.parent_id, [])
        if record.child.id in siblings:
            siblings.remove(record.child.id)
        await self.agent_manager.event_bus.emit(
            "subagent/disposed",
            {"parent_id": str(record.parent_id), "child_id": str(record.child.id)},
        )

    async def detach(self, agent_id: SessionId) -> None:
        """处理用户直接 dispose 一个 child 的场景，清理对应 watcher 和索引。"""

        record = self._records.get(agent_id)
        if record is None:
            return
        record.disposed = True
        if record.watcher is not None and record.watcher is not asyncio.current_task():
            record.watcher.cancel()
            await asyncio.gather(record.watcher, return_exceptions=True)
        self._records.pop(agent_id, None)
        siblings = self._children.get(record.parent_id, [])
        if agent_id in siblings:
            siblings.remove(agent_id)
        await self.agent_manager.event_bus.emit(
            "subagent/disposed",
            {"parent_id": str(record.parent_id), "child_id": str(agent_id)},
        )


__all__ = ["SubagentManager"]
