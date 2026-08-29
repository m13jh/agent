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

在 `python-agent` 项目根目录创建 `.env`（可以复制 `.env.example`），配置连接参数：

```dotenv
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_TIMEOUT_SECONDS=120
DEEPSEEK_MODEL=deepseek-chat
```

代码会优先读取构造函数显式参数，其次读取已经存在的环境变量，最后读取项目 `.env`。
`.env` 默认不会覆盖已经由 Shell 或 Conda 设置的同名环境变量。建议保护密钥文件：

```bash
chmod 600 .env
```

配置完成后运行：

```bash
python-agent run "检查项目结构" --provider deepseek
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

## 阶段 2 Agent Handle

阶段 2提供由 `AgentManager` 持有的 Agent Handle。`followup` 开启新的 Turn，`steer`
在下一个 Step 注入纠偏信息，`inject` 只写入上下文但不会唤醒 idle Agent：

```python
import asyncio

from python_agent import AgentManager, FakeAdapter


async def main() -> None:
    manager = AgentManager()
    agent = await manager.create(FakeAdapter())
    await agent.inject("这是静默上下文")
    await agent.followup("请完成任务")
    await agent.when_idle()
    print(agent.last_result.answer)
    await manager.shutdown()


asyncio.run(main())
```

一个 Agent 同时只会拥有一个 Driver Task；所有 Inbox 操作都会记录为
`agent/inbox/spliced`，因此可以通过事件重放恢复待处理消息。真正的 JSONL 磁盘持久化
属于后续阶段。

## 阶段 3 工具策略与安全执行

所有工具调用都会经过三条 Waterfall：Pre 负责参数、路径、权限和审批，Execute 负责
超时，Post 负责结果规范化、裁剪和 spill。写入工具需要明确指定 workspace-write：

```bash
python-agent run "更新 README" \
  --permission-mode workspace-write
```

`bash` 默认需要审批；没有审批服务时会在工具主体执行前返回错误。只有在明确确认本次
进程中的所有 Bash 调用都可信时，才使用：

```bash
python-agent run "检查项目构建" \
  --permission-mode workspace-write \
  --approve-bash
```

超长工具结果会保存到 workspace 下的 `.python-agent/tool-output/`，模型上下文只收到
预览、总长度和文件路径。应用层也可以传入自己的 `ApprovalService`、Pre/Execute/Post
策略或 `spill_directory`。

CLI 会实时显示模型文本增量、工具调用和工具结果。DeepSeek 使用 SSE 流式接口；Fake
Adapter 也会切分文本，用于离线验证同样的显示流程。

## 交互式终端

在真实终端中先激活 `agent` 环境，再启动 chat：

```bash
conda activate agent
python-agent chat
```

交互模式支持以下输入：

```text
普通文本             → followup，开启新的 Turn
/steer 内容          → 在下一步纠偏
/inject 内容         → 写入上下文但不唤醒 idle Agent
/cancel              → 取消并清空待处理输入
/cancel keep         → 取消但保留 Inbox
/status              → 查看 Agent 和 Inbox 状态
/transcript          → 查看当前 Session
/wait                → 等待当前任务完成
/exit                → 退出
```

模型在后台运行时仍然可以输入下一条 followup 或 steer；实时输出由 Live Event Bus
转发到终端。

## 待改进清单

当前问题、根因、优先级、参考 `deepseek-harness` 的实现和验收标准见
[IMPROVEMENTS.md](IMPROVEMENTS.md)。
