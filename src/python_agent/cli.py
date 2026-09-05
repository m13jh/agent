"""阶段 1—7 Agent 的 Codex 风格交互入口与兼容管理命令。

``run`` 负责一次性任务，``chat`` 负责长期交互。两者共享同一套 AgentManager、工具
注册表、权限配置和 Live Event Bus，因此命令行只是接入层，不重复实现 Agent 逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout

from python_agent.approval.service import (
    ApprovalRequest,
    ApprovalService,
    CallbackApprovalService,
    InteractiveApprovalService,
)
from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.core.agent_manager import AgentManager
from python_agent.core.lifecycle import CancelCause
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import CallId, SessionId, new_session_id
from python_agent.llm.adapter import ModelAdapter
from python_agent.llm.deepseek_adapter import DeepSeekAdapter
from python_agent.llm.fake_adapter import FakeAdapter, ResponseFactory
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall
from python_agent.session.compaction import ContextCompactor, StaticSummaryProvider
from python_agent.session.jsonl_store import JsonlSessionStore
from python_agent.session.sqlite_index import SqliteSessionIndex
from python_agent.skills.registry import SkillRegistry
from python_agent.skills.tool import ListSkillsTool, LoadSkillTool
from python_agent.terminal_ui import FullScreenTerminalUI, TerminalUI
from python_agent.tools.builtins import (
    ApplyPatchTool,
    BashTool,
    DeleteDirectoryTool,
    DeleteFileTool,
    EchoTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    WriteFileTool,
)
from python_agent.tools.container import ContainerExecTool
from python_agent.tools.registry import ToolRegistry

LiveHandler = Callable[[str, dict[str, Any]], None | Awaitable[None]]


def _display_event(event_type: str, data: dict[str, Any]) -> None:
    """把 Agent 的实时通知渲染成终端输出。

    ``assistant/delta`` 直接以无换行方式打印，所以用户能看到模型逐段生成；工具事件
    则主动换行并显示工具名和参数，避免工具信息和模型半截句子粘在同一行。工具结果
    只展示有限长度，完整结果仍然保存在 Session 的 ``tool/result`` 事件中。
    """

    if event_type == "model/request_start":
        raw_attempt = data.get("attempt", 1)
        attempt = raw_attempt if isinstance(raw_attempt, int) else 1
        attempt_text = f"，尝试 {attempt}" if attempt > 1 else ""
        print(
            f"\n[模型请求] Turn {data.get('turn')} / Step {data.get('step')} → "
            f"{data.get('provider')}/{data.get('model')}{attempt_text}，正在等待响应……",
            flush=True,
        )
        return
    if event_type == "model/request_error":
        if data.get("will_retry"):
            print(
                f"[请求错误] {data.get('error_type')}；将在 "
                f"{data.get('delay_seconds', 0)} 秒后重试。",
                flush=True,
            )
        return
    if event_type == "model/request_retry":
        print(
            f"[模型重试] 即将开始第 {data.get('next_attempt')} 次尝试：{data.get('reason')}",
            flush=True,
        )
        return
    if event_type == "agent/limit":
        reason = data.get("reason")
        if reason == "max_steps":
            print(
                f"\n[任务暂停] {data.get('message')}",
                flush=True,
            )
        else:
            print(
                f"\n[预算终止] {reason}：{data.get('message')}",
                flush=True,
            )
        return
    if event_type == "subagent/created":
        print(
            f"\n[子 Agent 创建] {data.get('child_id')}，深度 "
            f"{data.get('delegation_depth')}：{data.get('description')}",
            flush=True,
        )
        return
    if event_type == "subagent/settled":
        print(
            f"\n[子 Agent 完成] {data.get('child_id')}，结束原因：{data.get('finish_reason')}",
            flush=True,
        )
        return
    if event_type == "subagent/disposed":
        print(f"\n[子 Agent 释放] {data.get('child_id')}", flush=True)
        return
    if event_type == "subagent/error":
        print(
            f"\n[子 Agent 错误] {data.get('child_id')}："
            f"{data.get('error_type')} {data.get('message')}",
            flush=True,
        )
        return
    if event_type == "model/request_end":
        raw_duration = data.get("duration_ms", 0)
        duration_ms = raw_duration if isinstance(raw_duration, int | float) else 0
        status = data.get("status")
        if status == "completed":
            print(
                f"\n[模型完成] 用时 {duration_ms / 1000:.2f} 秒，"
                f"结束原因：{data.get('finish_reason')}",
                flush=True,
            )
        elif status == "cancelled":
            print(f"\n[模型取消] 已等待 {duration_ms / 1000:.2f} 秒。", flush=True)
        else:
            print(
                f"\n[模型失败] 用时 {duration_ms / 1000:.2f} 秒，"
                f"错误类型：{data.get('error_type')}",
                flush=True,
            )
        return
    if event_type == "assistant/delta":
        content = data.get("content")
        if isinstance(content, str):
            print(content, end="", flush=True)
        return
    if event_type == "assistant/message":
        # 流式响应已经逐段输出过，不能在这里把完整回答再打印一次。
        if data.get("streamed") is not True and isinstance(data.get("content"), str):
            print(data["content"], end="", flush=True)
        return
    if event_type == "tool/call":
        arguments = json.dumps(data.get("arguments", {}), ensure_ascii=False, sort_keys=True)
        print(f"\n\n[工具调用] {data.get('name')}\n参数：{arguments}", flush=True)
        return
    if event_type == "tool/result":
        content = data.get("content")
        rendered = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if len(rendered) > 2000:
            rendered = rendered[:2000] + "\n[终端显示已截断，Session 中保留完整结果]"
        print(f"[工具结果] {data.get('name')}：\n{rendered}", flush=True)
        return
    if event_type == "agent/error":
        print(
            f"\n[Agent 错误] {data.get('error_type')}: {data.get('message')}",
            flush=True,
        )


def _allow_explicit_bash(request: ApprovalRequest) -> bool:
    """实现 --approve-bash 的非交互授权，但不替代独立网络 Scope 批准。"""

    # 网络是和 Bash 分离的权限维度；即使调用方选择自动批准 Bash，也必须显式传入
    # --approve-network，避免一个旧的“允许执行命令”开关悄悄打开外网。
    return not request.reason.startswith("network Scope required")


def _demo_responder(path: str) -> ResponseFactory:
    """构造离线 demo 的响应工厂：第一次读文件，第二次引用工具结果回答。"""

    used_tool = False

    def respond(request: ModelRequest) -> AssistantResponse | dict[str, Any]:
        """根据当前请求处于第几步，返回工具调用或最终回答。"""

        nonlocal used_tool
        if not used_tool:
            used_tool = True
            return {
                "content": None,
                "tool_calls": [
                    ToolCall(id=CallId("demo-read"), name="read_file", arguments={"path": path})
                ],
                "finish_reason": "tool_calls",
            }
        tool_message = next(
            (message for message in reversed(request.messages) if message.get("role") == "tool"),
            {"content": ""},
        )
        return {
            "content": f"读取结果：\n{tool_message.get('content', '')}",
            "finish_reason": "stop",
        }

    return respond


def _add_agent_options(command: argparse.ArgumentParser) -> None:
    """给 run 和 chat 子命令添加一致的模型、工作区和执行限制参数。"""

    command.add_argument("--provider", default="fake", choices=("fake", "deepseek"))
    command.add_argument("--model", default=None)
    command.add_argument("--workspace", type=Path, default=Path.cwd())
    command.add_argument("--max-steps", type=int, default=30)
    command.add_argument("--max-parallel-tools", type=int, default=4)
    command.add_argument(
        "--max-turn-tokens",
        type=int,
        default=None,
        help="maximum cumulative model tokens in one Turn",
    )
    command.add_argument(
        "--max-turn-seconds",
        type=float,
        default=None,
        help="maximum wall-clock seconds in one Turn",
    )
    command.add_argument(
        "--max-turn-cost-usd",
        type=float,
        default=None,
        help="maximum estimated/provider-reported USD cost in one Turn",
    )
    command.add_argument("--input-cost-per-million-tokens", type=float, default=None)
    command.add_argument("--output-cost-per-million-tokens", type=float, default=None)
    command.add_argument("--model-max-retries", type=int, default=2)
    command.add_argument("--model-retry-base-delay-seconds", type=float, default=0.5)
    command.add_argument(
        "--enable-subagents",
        action="store_true",
        help="expose bounded in-process subagent management tools",
    )
    command.add_argument("--max-subagent-depth", type=int, default=2)
    command.add_argument("--max-subagents", type=int, default=8)
    command.add_argument(
        "--skills-root",
        type=Path,
        default=None,
        help="directory containing declarative on-demand skills",
    )
    command.add_argument(
        "--session-root",
        type=Path,
        default=None,
        help="Session storage root; defaults to WORKSPACE/.python-agent",
    )
    command.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="resume a persisted Session before submitting new input",
    )
    command.add_argument(
        "--repair-session",
        action="store_true",
        help="repair a provably incomplete crash tail while resuming",
    )
    command.add_argument(
        "--permission-mode",
        choices=("read-only", "workspace-write"),
        default="read-only",
        help="whether write_file, apply_patch and bash may run",
    )
    command.add_argument(
        "--permission-level",
        choices=("L0", "L1", "L2", "L3", "L4"),
        default=None,
        help="explicit SANBOX permission level; defaults to L0/L1 from permission mode",
    )
    command.add_argument(
        "--network-mode",
        choices=("disabled", "setup-approved", "allowlist", "full"),
        default="disabled",
        help="independent network scope for sandboxed commands",
    )
    command.add_argument(
        "--approve-network",
        action="store_true",
        help="approve the current short-lived network scope when a network mode requests it",
    )
    command.add_argument(
        "--enable-container",
        action="store_true",
        help="expose the fixed L3 container_exec tool",
    )
    command.add_argument(
        "--approve-bash",
        action="store_true",
        help="non-interactively approve bash and other high-risk calls for this process",
    )


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器；子命令解析结果最终交给异步运行函数。"""

    parser = argparse.ArgumentParser(prog="python-agent", description="Run a small Python agent")
    parser.add_argument("--version", action="version", version="python-agent 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run one user task")
    run.add_argument("prompt", help="the user task")
    _add_agent_options(run)
    run.add_argument("--show-events", action="store_true")
    run.add_argument(
        "--demo-read",
        metavar="PATH",
        help="offline demo: call read_file before answering",
    )
    chat = subparsers.add_parser("chat", help="start an interactive agent session")
    chat.add_argument("prompt", nargs="?", help="optional initial task, then remain interactive")
    chat.add_argument("--plain", action="store_true", help="disable full-screen terminal rendering")
    _add_agent_options(chat)

    sessions = subparsers.add_parser("sessions", help="list persisted Sessions")
    sessions.add_argument("--session-root", type=Path, default=Path(".python-agent"))

    transcript = subparsers.add_parser("transcript", help="export a Session transcript")
    transcript.add_argument("session_id")
    transcript.add_argument("output", type=Path)
    transcript.add_argument("--session-root", type=Path, default=Path(".python-agent"))

    repair = subparsers.add_parser("repair", help="repair a recoverable Session crash tail")
    repair.add_argument("session_id")
    repair.add_argument("--session-root", type=Path, default=Path(".python-agent"))

    fork = subparsers.add_parser("fork", help="fork a persisted Session into an independent branch")
    fork.add_argument("session_id")
    fork.add_argument("--target-id")
    fork.add_argument("--session-root", type=Path, default=Path(".python-agent"))

    compact = subparsers.add_parser("compact", help="append a reviewed context summary")
    compact.add_argument("session_id")
    summary = compact.add_mutually_exclusive_group(required=True)
    summary.add_argument("--summary")
    summary.add_argument("--summary-file", type=Path)
    compact.add_argument("--keep-recent-turns", type=int, default=2)
    compact.add_argument("--session-root", type=Path, default=Path(".python-agent"))

    index_sessions = subparsers.add_parser(
        "index-sessions", help="rebuild a derived SQLite cross-session index"
    )
    index_sessions.add_argument("--session-root", type=Path, default=Path(".python-agent"))
    index_sessions.add_argument("--index", type=Path, default=Path(".python-agent/index.sqlite3"))

    search_sessions = subparsers.add_parser(
        "search-sessions", help="search the derived SQLite session index"
    )
    search_sessions.add_argument("query")
    search_sessions.add_argument("--index", type=Path, default=Path(".python-agent/index.sqlite3"))
    search_sessions.add_argument("--session-id")
    search_sessions.add_argument("--limit", type=int, default=20)
    return parser


def _session_root(value: Path | None, workspace: Path) -> Path:
    """把相对 Session 根解释为 workspace 内路径，并返回规范化绝对路径。"""

    if value is None:
        return (workspace / ".python-agent").resolve()
    return value.expanduser().resolve() if value.is_absolute() else (workspace / value).resolve()


async def _create_agent(
    args: argparse.Namespace,
    *,
    event_handler: LiveHandler | None = None,
    approval_service: ApprovalService | None = None,
) -> tuple[AgentManager, Agent]:
    """根据命令行参数创建 Manager、共享事件总线、工具集合和 Agent Handle。"""

    # CLI 统一注册全部阶段 1—3工具；能否执行写工具或 Bash，由后续的权限和审批策略决定。
    registry = ToolRegistry(
        [
            ReadFileTool(),
            ListFilesTool(),
            SearchTextTool(),
            EchoTool(),
            WriteFileTool(),
            ApplyPatchTool(),
            BashTool(),
            DeleteFileTool(),
            DeleteDirectoryTool(),
        ]
    )
    workspace = args.workspace.expanduser().resolve()
    # workspace 是调用方明确选择的项目边界；如果路径尚不存在，初始化它可以让根目录
    # 文件写入和 Docker bind mount 使用同一语义，而不会在工具成功后再因挂载/manifest
    # 失败产生“实际已创建但返回错误”的假失败。
    workspace.mkdir(parents=True, exist_ok=True)
    skills_root: Path | None = None
    if args.skills_root is not None:
        skills_root = (
            args.skills_root.expanduser().resolve()
            if args.skills_root.is_absolute()
            else (workspace / args.skills_root).resolve()
        )
        skill_registry = SkillRegistry(skills_root)
        registry.register(ListSkillsTool(skill_registry))
        registry.register(LoadSkillTool(skill_registry, registry))
    if args.enable_container:
        registry.register(ContainerExecTool())
    if args.provider == "deepseek":
        # DeepSeekAdapter 在初始化时读取 .env；model 再从命令行、环境变量或默认值解析。
        adapter: ModelAdapter = DeepSeekAdapter()
        model = args.model or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    else:
        # FakeAdapter 不访问网络，适合离线运行、CI 和本地验证 Agent 生命周期。
        demo_read = getattr(args, "demo_read", None)
        adapter = FakeAdapter(_demo_responder(demo_read) if demo_read else None)
        model = args.model or "fake-model"
    # CLI 没有独立的 preset 配置文件，因此把影响恢复能力的关键选项编码进稳定 ID。
    # 用户若用不同 Provider、模型、权限或步数恢复，Manager 会因 ID 不匹配而明确拒绝。
    preset_id = (
        f"cli-v1:{args.provider}:{model}:{args.permission_mode}:steps={args.max_steps}"
        f":level={args.permission_level or '-'}:network={args.network_mode}"
    )
    phase5_values = (
        args.max_parallel_tools,
        args.max_turn_tokens,
        args.max_turn_seconds,
        args.max_turn_cost_usd,
        args.input_cost_per_million_tokens,
        args.output_cost_per_million_tokens,
        args.model_max_retries,
        args.model_retry_base_delay_seconds,
    )
    # 非默认阶段五限制属于能力集合的一部分，编码进 preset ID，避免恢复时静默换预算。
    if phase5_values != (4, None, None, None, None, None, 2, 0.5):
        preset_id += ":p5=" + ",".join(
            "none" if value is None else str(value) for value in phase5_values
        )
    if args.enable_subagents:
        preset_id += f":p6=depth{args.max_subagent_depth},children{args.max_subagents}"
    if skills_root is not None:
        preset_id += f":skills={skills_root}"
    if args.approve_network:
        preset_id += ":network-approved"
    if args.enable_container:
        preset_id += ":container"
    config = AgentPreset(
        id=preset_id,
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        max_parallel_tools=args.max_parallel_tools,
        max_turn_tokens=args.max_turn_tokens,
        max_turn_seconds=args.max_turn_seconds,
        max_turn_cost_usd=args.max_turn_cost_usd,
        input_cost_per_million_tokens=args.input_cost_per_million_tokens,
        output_cost_per_million_tokens=args.output_cost_per_million_tokens,
        model_max_retries=args.model_max_retries,
        model_retry_base_delay_seconds=args.model_retry_base_delay_seconds,
        subagents_enabled=args.enable_subagents,
        max_delegation_depth=args.max_subagent_depth,
        max_subagents=args.max_subagents,
        skills_root=skills_root,
        workspace=workspace,
        permission_mode=args.permission_mode,
        permission_level=args.permission_level,
        network_mode=args.network_mode,
        network_scope_approved=args.approve_network,
    )
    if approval_service is None and args.approve_bash:
        approval_service = CallbackApprovalService(_allow_explicit_bash)
    event_bus = LiveEventBus()
    event_bus.subscribe("*", event_handler or _display_event)
    store = JsonlSessionStore(_session_root(args.session_root, workspace))
    manager = AgentManager(event_bus=event_bus, session_store=store)
    if args.resume:
        agent = await manager.resume(
            SessionId(args.resume),
            adapter,
            registry,
            config=config,
            repair=args.repair_session,
            approval_service=approval_service,
        )
    else:
        if args.repair_session:
            raise ValueError("--repair-session requires --resume SESSION_ID")
        agent = await manager.create(
            adapter,
            registry,
            config=config,
            approval_service=approval_service,
        )
    return manager, agent


async def _run(args: argparse.Namespace) -> int:
    """执行一次性任务，并在打印结果后释放 Manager 所有的 Agent 资源。"""

    manager, agent = await _create_agent(args)
    try:
        await agent.followup(args.prompt)
        await agent.when_idle()
        result = agent.last_result
        if result is None:
            raise RuntimeError("agent became idle without a result")
        # 流式内容在事件回调中已经输出，这里只补一个换行，避免 Shell 提示符紧贴答案。
        print()
        # Session ID 写到 stderr，不混入模型最终回答的 stdout；调用方仍可复制它用于恢复。
        print(f"[Session] {result.session_id}", file=sys.stderr)
        if args.show_events:
            # 事件输出是诊断视图，不参与模型上下文，也不会改变已完成的 Session。
            print("\n--- session events ---")
            for event in result.session.events:
                print(event.model_dump_json())
    finally:
        await manager.shutdown()
    return 0


async def _list_persisted_sessions(args: argparse.Namespace) -> int:
    """列出 Store 中的 Header，不加载模型或创建 Agent。"""

    store = JsonlSessionStore(args.session_root.expanduser().resolve())
    headers = await store.list()
    if not headers:
        print("（没有持久化 Session）")
        return 0
    for header in headers:
        print(
            f"{header.id}\t{header.created_at.isoformat()}\t"
            f"preset={header.agent_preset or '-'}\tcwd={header.cwd or '-'}"
        )
    return 0


async def _export_persisted_transcript(args: argparse.Namespace) -> int:
    """从严格校验后的事件日志导出 transcript。"""

    store = JsonlSessionStore(args.session_root.expanduser().resolve())
    output = await store.export_transcript(SessionId(args.session_id), args.output)
    print(output)
    return 0


async def _repair_persisted_session(args: argparse.Namespace) -> int:
    """显式执行保守修复，并输出便于审计的结构化报告。"""

    store = JsonlSessionStore(args.session_root.expanduser().resolve())
    report = await store.repair(SessionId(args.session_id))
    print(
        json.dumps(
            {
                "changed": report.changed,
                "tail_action": report.tail.action,
                "backup_path": (
                    str(report.tail.backup_path) if report.tail.backup_path is not None else None
                ),
                "recovered_call_ids": list(report.semantic.recovered_call_ids),
                "closed_step": report.semantic.closed_step,
                "closed_turn": report.semantic.closed_turn,
                "appended_event_seqs": list(report.semantic.appended_event_seqs),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def _fork_persisted_session(args: argparse.Namespace) -> int:
    """复制事件快照创建独立 Session 分支。"""

    store = JsonlSessionStore(args.session_root.expanduser().resolve())
    target_id = SessionId(args.target_id) if args.target_id else new_session_id()
    forked = await store.fork(SessionId(args.session_id), target_id)
    print(forked.id)
    return 0


async def _compact_persisted_session(args: argparse.Namespace) -> int:
    """使用用户审阅摘要压缩完整旧 Turn 的模型可见表面。"""

    store = JsonlSessionStore(args.session_root.expanduser().resolve())
    session = await store.load(SessionId(args.session_id))
    if args.summary_file is not None:
        summary = args.summary_file.expanduser().read_text(encoding="utf-8")
    else:
        summary = args.summary
    result = await ContextCompactor().compact(
        session,
        StaticSummaryProvider(summary),
        keep_recent_turns=args.keep_recent_turns,
    )
    if result is None:
        print("没有新的完整旧 Turn 需要压缩。")
    else:
        print(
            f"summary_event_seq={result.event.seq} "
            f"replaced_turns={list(result.replaced_turns)} "
            f"replaced_events={result.replaced_event_count}"
        )
    return 0


async def _index_persisted_sessions(args: argparse.Namespace) -> int:
    """从严格 JSONL 真相源重建派生 SQLite 索引。"""

    store = JsonlSessionStore(args.session_root.expanduser().resolve())
    index = SqliteSessionIndex(args.index.expanduser().resolve())
    count = await index.rebuild(store)
    print(f"indexed_sessions={count} index={index.path}")
    return 0


async def _search_persisted_sessions(args: argparse.Namespace) -> int:
    """查询 SQLite 派生索引并逐行输出 JSON 命中。"""

    index = SqliteSessionIndex(args.index.expanduser().resolve())
    session_id = SessionId(args.session_id) if args.session_id else None
    hits = index.search(args.query, limit=args.limit, session_id=session_id)
    for hit in hits:
        print(hit.model_dump_json())
    return 0


def _chat_output(
    ui: TerminalUI | None,
    message: str,
    *,
    style: str = "dim",
) -> None:
    """把交互命令反馈路由到 Rich UI 或纯文本回退。"""

    if ui is None:
        print(message)
    else:
        ui.print_notice(message, style=style)


def _short_approval_value(value: Any, *, maximum: int = 240) -> str:
    """把审批摘要压成单行，避免把大段工具参数或文件内容直接刷到终端。"""

    text = " ".join(str(value).split())
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def _format_approval_request(request: ApprovalRequest) -> str:
    """生成不包含 write_file 全文的可读审批提示。"""

    arguments = request.arguments
    details: str
    if request.tool_name in {"bash", "container_exec", "docker_exec"}:
        details = f"命令：{_short_approval_value(arguments.get('command', ''))}"
    elif request.tool_name == "write_file":
        content = arguments.get("content", "")
        size = len(content.encode("utf-8")) if isinstance(content, str) else "?"
        details = f"路径：{arguments.get('path', '')}；写入字节：{size}（内容不在审批提示中显示）"
    elif request.tool_name == "apply_patch":
        patch = str(arguments.get("patch", ""))
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
        details = f"目标：{', '.join(paths) or 'workspace patch'}"
    elif request.tool_name in {"delete_file", "delete_directory"}:
        details = f"目标：{arguments.get('path', '')}"
    else:
        details = f"参数键：{', '.join(sorted(str(key) for key in arguments)) or '无'}"
    return (
        f"[需要审批] {request.tool_name}\n"
        f"原因：{request.reason}\n"
        f"{details}\n"
        "输入 y/yes 批准，n/no 拒绝；输入 /exit 可退出并拒绝未完成审批。"
    )


def _interactive_approval_notice(
    ui: TerminalUI | None,
) -> Callable[[ApprovalRequest], None]:
    """创建只负责显示审批请求的回调；stdin 仍由 chat 主循环独占。"""

    def notify(request: ApprovalRequest) -> None:
        _chat_output(ui, _format_approval_request(request), style="yellow")

    return notify


def _print_chat_help(ui: TerminalUI | None = None) -> None:
    """显示交互式终端支持的特殊命令。"""

    if ui is not None:
        ui.show_help()
        return
    print(
        """\n可用命令：
  /steer 内容       在下一步纠偏
  /inject 内容      写入上下文但不唤醒 idle Agent
  /cancel           取消当前执行并清空待处理输入
  /cancel keep      取消当前执行但保留 Inbox
  /continue         继续上一个因执行限制暂停的任务
  /status           查看 Agent 状态和 Inbox
  /transcript       查看当前 Session transcript
  /tools            展开或折叠工具参数、命令和结果
  /wait             等待当前任务回到 idle
  /exit             退出交互模式
直接输入其他文本会调用 followup，开启一个新的 Turn。
“你>”是终端提示符，无需手动输入；误粘贴时会自动移除。
Agent 运行中的 followup 会显示“已排队”，并在当前任务结束后按顺序处理。
需要人工确认时输入 y/yes 批准或 n/no 拒绝；--approve-bash 会关闭这类 Bash/高风险工具询问。
"""
    )


_CHAT_PROMPT_PREFIX = re.compile(r"^(?:你>\s*)+")
_CHAT_COMPLETER = WordCompleter(
    [
        "/help",
        "/status",
        "/transcript",
        "/tools",
        "/verbose",
        "/wait",
        "/cancel",
        "/cancel keep",
        "/continue",
        "/steer",
        "/inject",
        "/exit",
    ],
    sentence=True,
)


def _normalize_chat_line(raw_line: str) -> tuple[str, int]:
    """清理从终端示例中误复制进输入框的一个或多个 ``你>`` 提示符。

    ``PromptSession`` 返回的文本本来不包含提示符，但用户从文档或终端复制整行时可能把
    它一起粘贴回来。若不处理，``你> /transcript`` 会被当成普通 followup 发送给模型。
    返回移除数量是为了让 UI 明确告知发生了自动修正，而不是静默篡改输入。
    """

    stripped = raw_line.strip()
    matched = _CHAT_PROMPT_PREFIX.match(stripped)
    if matched is None:
        return stripped, 0
    prefix = matched.group(0)
    return stripped[matched.end() :].strip(), prefix.count("你>")


async def _dispatch_chat_line(
    agent: Agent,
    raw_line: str,
    ui: TerminalUI | None = None,
    approval_service: InteractiveApprovalService | None = None,
) -> bool:
    """解析并执行一行交互输入；返回 True 表示调用方应退出 REPL。

    把命令分发从 prompt 读取循环中抽离后，提示符清理、排队反馈和状态展示都可以进行
    确定性的单元测试。普通 followup 在 Agent 已运行时只入队，因此这里必须明确打印
    ``已排队``，避免用户因没有立即出现第二个模型回答而反复提交同一问题。
    """

    line, removed_prompts = _normalize_chat_line(raw_line)
    if removed_prompts:
        _chat_output(
            ui,
            f"[输入修正] 已移除 {removed_prompts} 个误复制的“你>”提示符；"
            "以后只需输入提示符后面的内容。",
            style="yellow",
        )
    if not line:
        return False
    if approval_service is not None and approval_service.has_pending:
        if line == "/exit":
            approval_service.close()
            _chat_output(ui, "[审批] 已拒绝未完成请求，正在退出。", style="yellow")
            return True
        normalized = line.casefold()
        if normalized in {"y", "yes", "是", "同意", "允许", "/approve", "/approve yes"}:
            approval_request = approval_service.pending_request
            approval_service.respond(True)
            _chat_output(
                ui,
                f"[审批] {approval_request.tool_name if approval_request is not None else '请求'} "
                "已批准。",
                style="green",
            )
            return False
        if normalized in {"n", "no", "否", "拒绝", "不允许", "/deny", "/deny no"}:
            approval_request = approval_service.pending_request
            approval_service.respond(False)
            _chat_output(
                ui,
                f"[审批] {approval_request.tool_name if approval_request is not None else '请求'} "
                "已拒绝。",
                style="yellow",
            )
            return False
        _chat_output(ui, "[审批] 当前有待处理请求，请输入 y/yes 或 n/no。", style="yellow")
        return False
    if line in {"/exit", "/quit"}:
        return True
    if line == "/help":
        _print_chat_help(ui)
        return False
    if line == "/steer":
        _chat_output(ui, "用法：/steer 内容", style="yellow")
        return False
    if line.startswith("/steer "):
        was_running = agent.status == "running"
        await agent.steer(line.removeprefix("/steer ").strip())
        pending = len(agent.inbox.pending("next_step"))
        if was_running:
            _chat_output(ui, f"[已排队] steer 将在下一个 Step 生效；next_step 当前 {pending} 条。")
        else:
            _chat_output(ui, "[已提交] steer 已唤醒 Agent。", style="cyan")
        return False
    if line == "/inject":
        _chat_output(ui, "用法：/inject 内容", style="yellow")
        return False
    if line.startswith("/inject "):
        await agent.inject(line.removeprefix("/inject ").strip())
        pending = len(agent.inbox.pending("next_step"))
        _chat_output(
            ui, f"[已注入] 静默上下文已保存；next_step 当前 {pending} 条，不会单独唤醒 Agent。"
        )
        return False
    if line == "/cancel" or line == "/cancel keep":
        keep_inbox = line == "/cancel keep"
        await agent.cancel(
            CancelCause(kind="user", message="交互终端取消"),
            keep_inbox=keep_inbox,
        )
        if keep_inbox:
            pending = len(agent.inbox.pending())
            _chat_output(
                ui, f"[已取消] 当前执行已停止，保留 {pending} 条 Inbox 消息。", style="yellow"
            )
        else:
            _chat_output(ui, "[已取消] 当前执行已停止，待处理 Inbox 已清空。", style="yellow")
        return False
    if line == "/continue":
        if agent.status == "running":
            _chat_output(
                ui,
                "[无法继续] Agent 当前仍在运行；请等待结束后再使用 /continue。",
                style="yellow",
            )
            return False
        if agent.task_status != "paused":
            _chat_output(ui, "[无法继续] 当前没有因执行限制暂停的任务。", style="yellow")
            return False
        await agent.continue_task()
        _chat_output(ui, "[已提交] 正在继续上一个尚未完成的任务。", style="cyan")
        return False
    if line == "/status":
        if ui is not None:
            ui.show_status(agent)
            return False
        request = agent.active_request
        details = [
            f"状态：{agent.status}",
            f"任务状态：{agent.task_status}",
            f"next_turn：{len(agent.inbox.pending('next_turn'))} 条",
            f"next_step：{len(agent.inbox.pending('next_step'))} 条",
        ]
        if request is not None:
            attempt_text = f"，尝试 {request.attempt}" if request.attempt > 1 else ""
            details.append(
                f"模型请求：Turn {request.turn} / Step {request.step} → "
                f"{request.provider}/{request.model}{attempt_text}，"
                f"已等待 {request.elapsed_seconds:.1f} 秒"
            )
        elif agent.status == "running":
            details.append("模型请求：当前无活动请求，可能正在执行工具或切换 Step")
        else:
            details.append("模型请求：无")
        print("\n".join(details))
        return False
    if line == "/transcript":
        transcript = agent.session.transcript()
        if ui is None:
            print(transcript or "（当前没有 transcript）")
        else:
            ui.show_transcript(transcript)
        return False
    if line in {"/tools", "/verbose"}:
        if ui is None:
            _chat_output(ui, "工具详情切换只在全屏界面生效；去掉 --plain 后使用 /tools。")
        else:
            ui.toggle_tool_details()
        return False
    if line == "/wait":
        try:
            await agent.when_idle()
        except Exception as exc:
            # Agent/error 已由事件总线实时展示；这里保留 REPL，并给出 wait 的收敛结果。
            _chat_output(ui, f"[等待结束] Agent 因 {type(exc).__name__} 结束：{exc}", style="red")
        else:
            _chat_output(ui, "Agent 已回到 idle。", style="green")
        return False

    was_running = agent.status == "running"
    was_paused = not was_running and agent.task_status == "paused"
    await agent.followup(line)
    if was_running:
        pending = len(agent.inbox.pending("next_turn"))
        _chat_output(
            ui, f"[已排队] followup 已保存；next_turn 当前 {pending} 条，当前任务结束后处理。"
        )
    elif was_paused:
        _chat_output(
            ui,
            "[新任务] 上一个任务因执行限制暂停，尚未生成最终回答；"
            "当前已开始新的 Turn，如需继续上一个任务请使用 /continue。",
            style="yellow",
        )
    else:
        _chat_output(ui, "[已提交] followup 已唤醒 Agent。", style="cyan")
    return False


async def _chat(args: argparse.Namespace) -> int:
    """在真实 TTY 使用单 renderer 全屏 TUI，其他环境回退纯文本 REPL。"""

    use_full_screen = not args.plain and sys.stdin.isatty() and sys.stdout.isatty()
    if use_full_screen:
        ui = FullScreenTerminalUI()
        interactive_approval = (
            None
            if args.approve_bash
            else InteractiveApprovalService(_interactive_approval_notice(ui))
        )
        manager, agent = await _create_agent(
            args,
            event_handler=ui.handle_event,
            approval_service=interactive_approval,
        )

        async def dispatch(line: str) -> bool:
            return await _dispatch_chat_line(agent, line, ui, interactive_approval)

        try:
            await ui.run(agent, dispatch, initial_prompt=args.prompt)
        finally:
            if interactive_approval is not None:
                interactive_approval.close()
            if agent.status == "running":
                await agent.cancel(CancelCause(kind="user", message="退出交互终端"))
            await manager.shutdown()
            ui.show_goodbye()
        return 0

    interactive_approval = None
    if not args.approve_bash:
        interactive_approval = InteractiveApprovalService(_interactive_approval_notice(None))
    manager, agent = await _create_agent(args, approval_service=interactive_approval)
    prompt_session: PromptSession[str] = PromptSession(
        message="你> ",
        history=InMemoryHistory(),
        completer=_CHAT_COMPLETER,
        auto_suggest=AutoSuggestFromHistory(),
        complete_while_typing=False,
    )
    print(f"python-agent 交互模式，Session：{agent.id}，输入 /help 查看命令。")
    try:
        should_exit = False
        if args.prompt:
            print(f"你> {args.prompt}")
            should_exit = await _dispatch_chat_line(
                agent,
                args.prompt,
                approval_service=interactive_approval,
            )
        with patch_stdout():
            while not should_exit:
                try:
                    raw_line = await prompt_session.prompt_async()
                except EOFError:
                    print("正在退出……")
                    break
                except KeyboardInterrupt:
                    if agent.status == "running":
                        await agent.cancel(
                            CancelCause(kind="user", message="Ctrl-C 取消当前执行"),
                            keep_inbox=False,
                        )
                        print("已取消当前执行。")
                    else:
                        print("输入 /exit 或按 Ctrl-D 退出。")
                    continue
                if await _dispatch_chat_line(
                    agent,
                    raw_line,
                    approval_service=interactive_approval,
                ):
                    break
    finally:
        if interactive_approval is not None:
            interactive_approval.close()
        if agent.status == "running":
            await agent.cancel(CancelCause(kind="user", message="退出交互终端"))
        await manager.shutdown()
    return 0


_CLI_COMMANDS = {
    "run",
    "chat",
    "sessions",
    "transcript",
    "repair",
    "fork",
    "compact",
    "index-sessions",
    "search-sessions",
}


def _normalize_cli_argv(argv: Sequence[str]) -> list[str]:
    """把 Codex 风格无子命令调用改写成 chat，同时保留原管理命令。

    ``python-agent``、``python-agent --provider ...`` 和 ``python-agent "任务"`` 都进入
    交互终端；显式 run/chat 及 Session 管理子命令保持向后兼容。
    """

    values = list(argv)
    if not values:
        return ["chat"]
    if values[0] in {"-h", "--help", "--version"} or values[0] in _CLI_COMMANDS:
        return values
    return ["chat", *values]


def main(argv: Sequence[str] | None = None) -> int:
    """同步 CLI 入口，负责选择子命令、启动事件循环和转换顶层异常。"""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_normalize_cli_argv(raw_argv))
    try:
        if args.command == "sessions":
            return asyncio.run(_list_persisted_sessions(args))
        if args.command == "transcript":
            return asyncio.run(_export_persisted_transcript(args))
        if args.command == "repair":
            return asyncio.run(_repair_persisted_session(args))
        if args.command == "fork":
            return asyncio.run(_fork_persisted_session(args))
        if args.command == "compact":
            return asyncio.run(_compact_persisted_session(args))
        if args.command == "index-sessions":
            return asyncio.run(_index_persisted_sessions(args))
        if args.command == "search-sessions":
            return asyncio.run(_search_persisted_sessions(args))
        if args.command == "chat":
            return asyncio.run(_chat(args))
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"python-agent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
