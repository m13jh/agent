"""阶段 1 Agent 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from python_agent.config import AgentPreset
from python_agent.core.agent_loop import AgentLoop
from python_agent.ids import CallId
from python_agent.llm.adapter import ModelAdapter
from python_agent.llm.deepseek_adapter import DeepSeekAdapter
from python_agent.llm.fake_adapter import FakeAdapter, ResponseFactory
from python_agent.llm.types import AssistantResponse, ModelRequest, ToolCall
from python_agent.tools.builtins import EchoTool, ListFilesTool, ReadFileTool, SearchTextTool
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


def _demo_responder(path: str) -> ResponseFactory:
    used_tool = False

    def respond(request: ModelRequest) -> AssistantResponse | dict[str, Any]:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python-agent", description="Run a small Python agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run one user task")
    run.add_argument("prompt", help="the user task")
    run.add_argument("--provider", default="fake", choices=("fake", "deepseek"))
    run.add_argument("--model", default=None)
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--max-steps", type=int, default=30)
    run.add_argument("--show-events", action="store_true")
    run.add_argument(
        "--demo-read",
        metavar="PATH",
        help="offline demo: call read_file before answering",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    registry = ToolRegistry([ReadFileTool(), ListFilesTool(), SearchTextTool(), EchoTool()])
    if args.provider == "deepseek":
        adapter: ModelAdapter = DeepSeekAdapter()
        model = args.model or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    else:
        adapter = FakeAdapter(_demo_responder(args.demo_read) if args.demo_read else None)
        model = args.model or "fake-model"
    config = AgentPreset(
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        workspace=args.workspace.resolve(),
    )
    result = await AgentLoop(
        adapter,
        registry,
        config=config,
        event_handler=_display_event,
    ).run(args.prompt)
    # 流式内容在事件回调中已经输出，这里只补一个换行，避免 Shell 提示符紧贴答案。
    print()
    if args.show_events:
        print("\n--- session events ---")
        for event in result.session.events:
            print(event.model_dump_json())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"python-agent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
