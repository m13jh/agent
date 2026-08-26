"""阶段 1 Agent 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
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
        model = args.model or "deepseek-chat"
    else:
        adapter = FakeAdapter(_demo_responder(args.demo_read) if args.demo_read else None)
        model = args.model or "fake-model"
    config = AgentPreset(
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        workspace=args.workspace.resolve(),
    )
    result = await AgentLoop(adapter, registry, config=config).run(args.prompt)
    print(result.answer)
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
