# python-agent

`python-agent` 是一个小而清晰的原生 Python Agent Harness。当前版本完成架构文档中的
“阶段 0 + 阶段 1”：它提供内存事件日志、模型适配器、串行工具运行时，以及一个可运行的
“模型 → 工具 → 模型”闭环。

## 安装

在本目录执行：

```bash
python -m pip install -e '.[dev]'
```

## 运行离线示例

默认使用可控的 Fake Adapter，因此不需要 API Key：

```bash
python-agent run "你好，介绍一下自己"
```

读取工作区内的文件：

```bash
python-agent run "读取 README.md" --demo-read README.md
```

## 使用 DeepSeek

DeepSeek 使用 OpenAI-compatible API。设置 `DEEPSEEK_API_KEY` 后运行：

```bash
python-agent run "检查项目结构" --provider deepseek --model deepseek-chat
```

也可以直接使用 Python API：

```python
import asyncio

from python_agent import AgentLoop, FakeAdapter
from python_agent.tools.builtins import EchoTool
from python_agent.tools.registry import ToolRegistry


async def main() -> None:
    registry = ToolRegistry([EchoTool()])
    agent = AgentLoop(FakeAdapter(), registry)
    result = await agent.run("hello")
    print(result.answer)
    print(result.session.transcript())


asyncio.run(main())
```

## 本地检查

```bash
ruff format --check .
ruff check .
mypy
pytest
```

当前仅实现阶段 1 的单次 `run(prompt)` API；durable inbox、steer/inject、JSONL 恢复和子
Agent 属于后续阶段。
