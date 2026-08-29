"""阶段 1—3 Agent 的命令行入口。

``run`` 负责一次性任务，``chat`` 负责长期交互。两者共享同一套 AgentManager、工具
注册表、权限配置和 Live Event Bus，因此命令行只是接入层，不重复实现 Agent 逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout

from python_agent.approval.service import ApprovalRequest, CallbackApprovalService
from python_agent.config import AgentPreset
from python_agent.core.agent import Agent
from python_agent.core.agent_manager import AgentManager
from python_agent.core.lifecycle import CancelCause
from python_agent.hooks.event_bus import LiveEventBus
from python_agent.ids import CallId
from python_agent.llm.adapter import ModelAdapter
from python_agent.llm.deepseek_adapter import DeepSeekAdapter
from python_agent.llm.fake_adapter import FakeAdapter, ResponseFactory
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall
from python_agent.tools.builtins import (
    ApplyPatchTool,
    BashTool,
    EchoTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    WriteFileTool,
)
from python_agent.tools.registry import ToolRegistry


def _display_event(event_type: str, data: dict[str, Any]) -> None:
    """把 Agent 的实时通知渲染成终端输出。

    ``assistant/delta`` 直接以无换行方式打印，所以用户能看到模型逐段生成；工具事件
    则主动换行并显示工具名和参数，避免工具信息和模型半截句子粘在同一行。工具结果
    只展示有限长度，完整结果仍然保存在 Session 的 ``tool/result`` 事件中。
    """

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


def _allow_explicit_bash(_request: ApprovalRequest) -> bool:
    """实现 --approve-bash 的明确授权；没有该开关时审批服务保持默认拒绝。"""

    return True


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
    command.add_argument(
        "--permission-mode",
        choices=("read-only", "workspace-write"),
        default="read-only",
        help="whether write_file, apply_patch and bash may run",
    )
    command.add_argument(
        "--approve-bash",
        action="store_true",
        help="explicitly approve all bash calls for this process",
    )


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器；子命令解析结果最终交给异步运行函数。"""

    parser = argparse.ArgumentParser(prog="python-agent", description="Run a small Python agent")
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
    _add_agent_options(chat)
    return parser


async def _create_agent(args: argparse.Namespace) -> tuple[AgentManager, Agent]:
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
        ]
    )
    if args.provider == "deepseek":
        # DeepSeekAdapter 在初始化时读取 .env；model 再从命令行、环境变量或默认值解析。
        adapter: ModelAdapter = DeepSeekAdapter()
        model = args.model or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    else:
        # FakeAdapter 不访问网络，适合离线运行、CI 和本地验证 Agent 生命周期。
        demo_read = getattr(args, "demo_read", None)
        adapter = FakeAdapter(_demo_responder(demo_read) if demo_read else None)
        model = args.model or "fake-model"
    config = AgentPreset(
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        workspace=args.workspace.resolve(),
        permission_mode=args.permission_mode,
    )
    approval_service = CallbackApprovalService(_allow_explicit_bash) if args.approve_bash else None
    event_bus = LiveEventBus()
    event_bus.subscribe("*", _display_event)
    manager = AgentManager(event_bus=event_bus)
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
        if args.show_events:
            # 事件输出是诊断视图，不参与模型上下文，也不会改变已完成的 Session。
            print("\n--- session events ---")
            for event in result.session.events:
                print(event.model_dump_json())
    finally:
        await manager.shutdown()
    return 0


def _print_chat_help() -> None:
    """显示交互式终端支持的特殊命令。"""

    print(
        """\n可用命令：
  /steer 内容       在下一步纠偏
  /inject 内容      写入上下文但不唤醒 idle Agent
  /cancel           取消当前执行并清空待处理输入
  /cancel keep      取消当前执行但保留 Inbox
  /status           查看 Agent 状态和 Inbox
  /transcript       查看当前 Session transcript
  /wait             等待当前任务回到 idle
  /exit             退出交互模式
直接输入其他文本会调用 followup，开启一个新的 Turn。
"""
    )


async def _chat(args: argparse.Namespace) -> int:
    """运行类似 REPL 的交互终端，让输入和后台 Driver 同时推进。

    prompt_async 不会阻塞 Agent 的事件循环，因此模型运行时用户仍然可以输入 steer、
    inject 或下一条 followup。patch_stdout 会在后台流式输出到达时重绘提示符，避免输出
    直接覆盖正在编辑的命令行。
    """

    manager, agent = await _create_agent(args)
    prompt_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
    print("python-agent 交互模式，输入 /help 查看命令。")
    try:
        with patch_stdout():
            while True:
                try:
                    raw_line = await prompt_session.prompt_async("你> ")
                except (EOFError, KeyboardInterrupt):
                    print("\n正在退出……")
                    break
                line = raw_line.strip()
                if not line:
                    continue
                if line in {"/exit", "/quit"}:
                    break
                if line == "/help":
                    _print_chat_help()
                elif line.startswith("/steer "):
                    # steer 不开启新 Turn，而是在最近的 Step 边界进入下一次模型请求。
                    await agent.steer(line.removeprefix("/steer ").strip())
                elif line.startswith("/inject "):
                    # inject 只入队；idle 时不会自行创建 Driver，避免静默上下文触发模型调用。
                    await agent.inject(line.removeprefix("/inject ").strip())
                elif line == "/cancel" or line == "/cancel keep":
                    await agent.cancel(
                        CancelCause(kind="user", message="交互终端取消"),
                        keep_inbox=line == "/cancel keep",
                    )
                    print("已请求取消当前执行。")
                elif line == "/status":
                    print(
                        f"状态：{agent.status}\n"
                        f"next_turn：{len(agent.inbox.pending('next_turn'))} 条\n"
                        f"next_step：{len(agent.inbox.pending('next_step'))} 条"
                    )
                elif line == "/transcript":
                    print(agent.session.transcript() or "（当前没有 transcript）")
                elif line == "/wait":
                    await agent.when_idle()
                    print("Agent 已回到 idle。")
                else:
                    # 普通文本统一视作 followup，交给 next_turn 队列；后台 Driver 会异步消费。
                    await agent.followup(line)
    finally:
        if agent.status == "running":
            await agent.cancel(CancelCause(kind="user", message="退出交互终端"))
        await manager.shutdown()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """同步 CLI 入口，负责选择子命令、启动事件循环和转换顶层异常。"""

    args = build_parser().parse_args(argv)
    try:
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
