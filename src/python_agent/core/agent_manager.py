"""AgentManager：统一拥有、创建和释放 Agent Handle。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from python_agent.approval.service import ApprovalService
from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.errors import ConfigurationError
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import SessionId, new_session_id
from python_agent.llm.adapter import ModelAdapter, ModelRouter
from python_agent.llm.retry import ModelRetryPolicy
from python_agent.session.events import SessionHeader
from python_agent.session.session import Session
from python_agent.session.store import SessionStore
from python_agent.subagents.manager import SubagentManager
from python_agent.tools.policies import ExecuteHandler, PostHandler, PreHandler
from python_agent.tools.registry import ToolRegistry


class AgentManager:
    """管理当前进程中的 Agent，并确保每个 Handle 都有明确生命周期所有者。"""

    def __init__(
        self,
        *,
        event_bus: LiveEventBus | None = None,
        session_store: SessionStore | None = None,
        presets: Mapping[str, AgentPreset] | None = None,
    ) -> None:
        """创建 Manager，并可绑定阶段四持久化 Store 与可恢复 preset。

        ``presets`` 按稳定 ID 保存完整能力配置。恢复时若调用方没有显式传入 config，
        Manager 只能使用这里登记且与 Header 同名的 preset；找不到时默认拒绝，避免旧会话
        在重启后悄悄获得不同模型、工具或权限。
        """

        self.event_bus = event_bus or LiveEventBus()
        self.session_store = session_store
        self._presets: dict[str, AgentPreset] = dict(presets or {})
        self._agents: dict[SessionId, Agent] = {}
        self.subagents = SubagentManager(self)

    def register_preset(self, preset: AgentPreset) -> None:
        """登记可用于恢复的不可变 preset；重复 ID 必须显式报错。"""

        if preset.id in self._presets:
            raise ConfigurationError(f"agent preset already registered: {preset.id}")
        self._presets[preset.id] = preset

    def _infrastructure_exclusions(self, explicit: tuple[Path, ...]) -> tuple[Path, ...]:
        """合并调用方排除项和 Store 根目录，供文件工具避免读取自身日志。"""

        paths = [path.expanduser().resolve() for path in explicit]
        store_root = getattr(self.session_store, "root", None)
        if isinstance(store_root, Path):
            resolved = store_root.expanduser().resolve()
            if resolved not in paths:
                paths.append(resolved)
        return tuple(paths)

    async def create(
        self,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        session: Session | None = None,
        system_prompt: str | None = None,
        workspace: Path | None = None,
        excluded_paths: tuple[Path, ...] = (),
        approval_service: ApprovalService | None = None,
        approval_required: set[str] | frozenset[str] | None = None,
        spill_directory: Path | None = None,
        pre_policies: tuple[PreHandler, ...] = (),
        execute_policies: tuple[ExecuteHandler, ...] = (),
        post_policies: tuple[PostHandler, ...] = (),
        request_retry_policy: ModelRetryPolicy | None = None,
        parent_session_id: SessionId | None = None,
        origin: Literal["user", "subagent"] = "user",
        delegation_depth: int = 0,
    ) -> Agent:
        """创建 Agent、登记所有权并发布 agent/created 通知。

        Manager 绑定 SessionStore 时，新 Session 会先在 Store 中原子创建，再交给 Agent；
        因而 Agent 构造完成后的第一条 Inbox 事件就已经具备持久化能力。
        """

        resolved_config = config or AgentPreset()
        if session is None:
            header = SessionHeader(
                id=new_session_id(),
                cwd=workspace or resolved_config.workspace,
                parent_session_id=parent_session_id,
                origin=origin,
                delegation_depth=delegation_depth,
                agent_preset=resolved_config.id,
            )
            session = (
                await self.session_store.create(header)
                if self.session_store is not None
                else Session(header)
            )

        agent = Agent(
            adapter,
            tools,
            config=resolved_config,
            session=session,
            system_prompt=system_prompt,
            workspace=workspace,
            excluded_paths=self._infrastructure_exclusions(excluded_paths),
            event_bus=self.event_bus,
            approval_service=approval_service,
            approval_required=approval_required,
            spill_directory=spill_directory,
            pre_policies=pre_policies,
            execute_policies=execute_policies,
            post_policies=post_policies,
            request_retry_policy=request_retry_policy,
        )
        await self._register_agent(agent)
        self.subagents.enable_for(agent)
        return agent

    async def _register_agent(self, agent: Agent) -> None:
        """登记唯一所有权并发布 created；失败前不会覆盖已有 Handle。"""

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

    async def create_agent(
        self,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        session: Session | None = None,
        system_prompt: str | None = None,
        workspace: Path | None = None,
        excluded_paths: tuple[Path, ...] = (),
        approval_service: ApprovalService | None = None,
        approval_required: set[str] | frozenset[str] | None = None,
        spill_directory: Path | None = None,
        pre_policies: tuple[PreHandler, ...] = (),
        execute_policies: tuple[ExecuteHandler, ...] = (),
        post_policies: tuple[PostHandler, ...] = (),
        request_retry_policy: ModelRetryPolicy | None = None,
    ) -> Agent:
        """create 的语义别名，方便调用方使用更明确的动词名称。"""

        return await self.create(
            adapter,
            tools,
            config=config,
            session=session,
            system_prompt=system_prompt,
            workspace=workspace,
            excluded_paths=excluded_paths,
            approval_service=approval_service,
            approval_required=approval_required,
            spill_directory=spill_directory,
            pre_policies=pre_policies,
            execute_policies=execute_policies,
            post_policies=post_policies,
            request_retry_policy=request_retry_policy,
        )

    def _resolve_resume_config(
        self,
        session: Session,
        explicit: AgentPreset | None,
    ) -> AgentPreset:
        """按 Header preset 名称恢复同一能力集合，禁止静默换配置。"""

        expected = session.header.agent_preset
        if explicit is not None:
            if expected is not None and explicit.id != expected:
                raise ConfigurationError(
                    f"session {session.id} requires preset {expected!r}, "
                    f"but {explicit.id!r} was supplied"
                )
            return explicit
        if expected is None:
            # 兼容阶段一到三创建、尚未记录 preset 的内存日志；这类日志只能使用安全默认值。
            return AgentPreset()
        try:
            return self._presets[expected]
        except KeyError as exc:
            raise ConfigurationError(
                f"cannot resume session {session.id}: preset {expected!r} is not registered"
            ) from exc

    async def resume(
        self,
        session_id: SessionId,
        adapter: ModelAdapter | ModelRouter,
        tools: ToolRegistry | None = None,
        *,
        config: AgentPreset | None = None,
        repair: bool = False,
        system_prompt: str | None = None,
        excluded_paths: tuple[Path, ...] = (),
        approval_service: ApprovalService | None = None,
        approval_required: set[str] | frozenset[str] | None = None,
        spill_directory: Path | None = None,
        pre_policies: tuple[PreHandler, ...] = (),
        execute_policies: tuple[ExecuteHandler, ...] = (),
        post_policies: tuple[PostHandler, ...] = (),
        request_retry_policy: ModelRetryPolicy | None = None,
    ) -> Agent:
        """加载持久 Session、重放 Inbox，并恢复仍可唤醒的 Driver。

        ``repair=False`` 是默认安全模式：物理半行、未闭合 Step 或工具调用都会明确失败。
        调用方确认是进程崩溃尾部后可传 ``repair=True``，Store 会先追加补偿事实，再发布
        Agent。恢复 Inbox 时也会找回“已 claim 但尚未写成 user/message”的孤立消息。
        """

        if self.session_store is None:
            raise ConfigurationError("AgentManager.resume requires a SessionStore")
        session = await self.session_store.load(session_id, repair=repair)
        resolved_config = self._resolve_resume_config(session, config)
        agent = Agent(
            adapter,
            tools,
            config=resolved_config,
            session=session,
            system_prompt=system_prompt,
            # workspace 必须来自持久化 Header，不能由恢复调用悄悄扩大。
            workspace=session.header.cwd,
            excluded_paths=self._infrastructure_exclusions(excluded_paths),
            event_bus=self.event_bus,
            approval_service=approval_service,
            approval_required=approval_required,
            spill_directory=spill_directory,
            pre_policies=pre_policies,
            execute_policies=execute_policies,
            post_policies=post_policies,
            request_retry_policy=request_retry_policy,
            recover_orphaned_claims=True,
        )
        await self._register_agent(agent)
        self.subagents.enable_for(agent)
        await agent.resume_pending()
        return agent

    async def fork_session(
        self,
        source_id: SessionId,
        target_id: SessionId | None = None,
    ) -> Session:
        """复制持久化事件快照创建独立分支，但不自动启动新 Agent。"""

        if self.session_store is None:
            raise ConfigurationError("AgentManager.fork_session requires a SessionStore")
        resolved_target = target_id or new_session_id()
        session = await self.session_store.fork(source_id, resolved_target)
        await self.event_bus.emit(
            "session/forked",
            {
                "source_session_id": str(source_id),
                "target_session_id": str(resolved_target),
            },
        )
        return session

    def get(self, agent_id: SessionId) -> Agent:
        """根据稳定 ID 返回 Agent；不存在时明确失败。"""

        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"agent not found: {agent_id}") from exc

    def maybe_get(self, agent_id: SessionId) -> Agent | None:
        """返回可选 Agent，供子 Agent watcher 在父级释放竞态中安全查询。"""

        return self._agents.get(agent_id)

    def list_agents(self) -> list[Agent]:
        """返回当前由 Manager 拥有的 Agent 快照。

        返回新列表而不是内部字典视图，避免调用方遍历期间修改 Manager 的所有权表。
        """

        return list(self._agents.values())

    async def dispose(self, agent_id: SessionId) -> None:
        """先 child-first 释放全部后代，再释放目标 Agent 和父子索引。"""

        self.get(agent_id)
        await self.subagents.dispose_descendants(agent_id)
        await self._dispose_agent_only(agent_id)
        await self.subagents.detach(agent_id)

    async def _dispose_agent_only(self, agent_id: SessionId) -> None:
        """只释放一个 Handle；递归顺序由 SubagentManager 或公开 dispose 负责。"""

        agent = self._agents.get(agent_id)
        if agent is None:
            return
        await agent.dispose()
        self._agents.pop(agent_id, None)

    async def shutdown(self) -> None:
        """应用退出时释放所有 Agent，防止遗留 Driver Task。"""

        # 优先从根开始会自然触发 child-first；异常状态下遗留的孤儿随后单独收敛。
        roots = [
            agent for agent in self.list_agents() if agent.session.header.parent_session_id is None
        ]
        for agent in roots:
            if agent.id in self._agents:
                await self.dispose(agent.id)
        for agent in self.list_agents():
            if agent.id in self._agents:
                await self.dispose(agent.id)
        self._agents.clear()


__all__ = ["AgentManager"]
